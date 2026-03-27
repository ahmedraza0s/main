import logging
import os
import json
import asyncio
import time
from datetime import datetime
from typing import Annotated, Optional, List
import aiofiles
import aiohttp

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions, llm, stt, tts, voice
from livekit.plugins import openai, cartesia, deepgram, noise_cancellation, sarvam, groq

from storage import CallMetrics, JsonFileStorage, TranscriptSegment

# Configure logging
logger = logging.getLogger("vobiz-agent-shared")

# Initialize storage
storage = JsonFileStorage()
STATUS_STORE_FILE = os.path.join("KMS", "logs", "active_calls.json")

async def update_call_status(call_id: str, status: str, phone: str = None):
    """Update or initialize the status of a call in the JSON file store asynchronouslly."""
    now = datetime.now().isoformat()
    try:
        data = {}
        if os.path.exists(STATUS_STORE_FILE):
            async with aiofiles.open(STATUS_STORE_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
                if content.strip():
                    data = json.loads(content)
        
        if call_id not in data:
            data[call_id] = {
                "call_id": call_id,
                "phone": phone or "unknown",
                "status": status,
                "triggered_at": now,
                "updated_at": now
            }
        else:
            data[call_id]["status"] = status
            data[call_id]["updated_at"] = now
            if phone:
                data[call_id]["phone"] = phone
        
        os.makedirs(os.path.dirname(STATUS_STORE_FILE), exist_ok=True)
        async with aiofiles.open(STATUS_STORE_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2))
            
        logger.info(f"Call {call_id} status updated to: {status}")
    except Exception as e:
        logger.error(f"Failed to update call status file: {e}")

async def send_webhook(metrics: CallMetrics, call_id: str):
    """Send call metrics and data to the configured webhook URL."""
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        return
    
    try:
        # Utilize the Pydantic model's JSON serializer to handle nested objects properly
        if hasattr(metrics, 'model_dump_json'):
            payload = json.loads(metrics.model_dump_json())
        else:
            payload = metrics.__dict__.copy()
        
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status >= 200 and response.status < 300:
                    logger.info(f"Successfully sent webhook for call {call_id} to {webhook_url}")
                else:
                    logger.error(f"Failed to send webhook for call {call_id}. Status: {response.status}, Response: {await response.text()}")
    except Exception as e:
        logger.error(f"Exception while sending webhook for call {call_id}: {e}")

def _build_tts():
    """Configure the Text-to-Speech provider based on env vars."""
    provider = os.getenv("TTS_PROVIDER", "openai").lower()
    if provider == "cartesia":
        return cartesia.TTS(
            model=os.getenv("CARTESIA_TTS_MODEL", "sonic-2"),
            voice=os.getenv("CARTESIA_TTS_VOICE", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
        )
    if provider == "sarvam":
        return sarvam.TTS(
            model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
            target_language_code=os.getenv("SARVAM_TARGET_LANGUAGE_CODE", "hi-IN"),
            api_key=os.getenv("SARVAM_API_KEY")
        )
    return openai.TTS(
        model=os.getenv("OPENAI_TTS_MODEL", "tts-1"),
        voice=os.getenv("OPENAI_TTS_VOICE", "alloy")
    )

def _build_llm():
    """Configure the LLM provider based on env vars."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider == "groq":
        return groq.LLM(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            api_key=os.getenv("GROQ_API_KEY")
        )
    return openai.LLM(model=os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini"))

class TransferFunctions(llm.ToolContext):
    def __init__(self, ctx: agents.JobContext, phone_number: str = None):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number
        self.SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN")

    @llm.function_tool(description="Transfer the call to a human support agent or another phone number.")
    async def transfer_call(self, destination: Optional[str] = None):
        """Transfer the call."""
        if destination is None:
            destination = os.getenv("DEFAULT_TRANSFER_NUMBER")
            if not destination:
                 return "Error: No default transfer number configured."
        
        if "@" not in destination:
            if self.SIP_DOMAIN:
                clean_dest = destination.replace("tel:", "").replace("sip:", "")
                destination = f"sip:{clean_dest}@{self.SIP_DOMAIN}"
            else:
                if not destination.startswith("tel:") and not destination.startswith("sip:"):
                     destination = f"tel:{destination}"
        elif not destination.startswith("sip:"):
             destination = f"sip:{destination}"
        
        logger.info(f"Transferring call to {destination}")
        participant_identity = None
        if self.phone_number:
            participant_identity = f"sip_{self.phone_number}"
        else:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        
        if not participant_identity:
            return "Failed to transfer: could not identify the caller."

        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False
                )
            )
            return "Transfer initiated successfully."
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return f"Error executing transfer: {e}"

def bind_metrics_events(session: AgentSession, metrics: CallMetrics, call_id: str):
    """Bind common metrics and transcript event handlers to the session."""
    
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev: voice.UserInputTranscribedEvent):
        if ev.is_final:
            text = ev.transcript
            metrics.transcript_segments.append(TranscriptSegment(
                text=text, speaker="user", timestamp=time.time()
            ))
            metrics.transcript += f"User: {text}\n"

    @session.on("conversation_item_added")
    def on_conversation_item_added(ev: voice.ConversationItemAddedEvent):
        if hasattr(ev.item, "role") and ev.item.role == "assistant" and ev.item.content:
            text = " ".join(ev.item.content) if isinstance(ev.item.content, list) else str(ev.item.content)
            metrics.transcript_segments.append(TranscriptSegment(
                text=text, speaker="agent", timestamp=time.time()
            ))
            metrics.transcript += f"Agent: {text}\n"

    @session.on("metrics_collected")
    def on_metrics_collected(ev: voice.MetricsCollectedEvent):
        m = ev.metrics
        if m.type == "stt_metrics":
            metrics.stt_latency.append(m.duration * 1000)
        elif m.type == "llm_metrics":
            metrics.llm_latency.append(m.ttft * 1000)
        elif m.type == "tts_metrics":
            metrics.tts_latency.append(m.ttfb * 1000)

    @session.on("error")
    def on_error(ev: voice.ErrorEvent):
        logger.error(f"Agent Error: {ev.error} from {ev.source}")
        asyncio.create_task(update_call_status(call_id, "failed"))

async def finalize_metrics(ctx: agents.JobContext, metrics: CallMetrics, call_id: str):
    """Finalize and save call metrics on session end."""
    logger.info(f"Saving call metrics for {ctx.room.name}...")
    metrics.call_end_time = datetime.now().isoformat()
    if metrics.call_status == "active":
        metrics.call_status = "completed"
        await update_call_status(call_id, "completed")
    try:
        start = datetime.fromisoformat(metrics.call_start_time)
        end = datetime.fromisoformat(metrics.call_end_time)
        metrics.call_duration = (end - start).total_seconds()
    except Exception:
        pass
    path = storage.save(metrics)
    logger.info(f"Metrics saved to {path}")
    
    # Send the webhook in the foreground to prevent asyncio event loop shutdown cancellation
    logger.info("Attempting to send webhook payload...")
    await send_webhook(metrics, call_id)
