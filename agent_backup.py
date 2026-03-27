# Webhook trigger endpoint
# Accepts POST /trigger-call from any external system (CRM, form, database trigger etc.)
# Validates API key from env, accepts phone + any number of custom variables
# Fires outbound call via existing LiveKit dispatch logic
# Fire and forget — nothing is sent back after the call triggers
# To deploy on VPS: only change BASE_URL in .env to your domain. Nothing else changes.

import logging
import os
import json
import asyncio
import time
import uuid
import threading
from datetime import datetime


from typing import Annotated, Optional

from dotenv import load_dotenv
from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions, llm, stt, tts, voice
from livekit.plugins import (
    openai,
    cartesia,
    deepgram,
    noise_cancellation,
    silero,
    sarvam,
    groq,
)
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
import uvicorn

from storage import CallMetrics, JsonFileStorage, TranscriptSegment

# Load environment variables
load_dotenv(".env")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# Initialize storage
storage = JsonFileStorage()

# File-based call status storage (reliable across processes on Windows)
STATUS_STORE_FILE = os.path.join("KMS", "logs", "active_calls.json")

def update_call_status(call_id: str, status: str, phone: str = None):
    """Update or initialize the status of a call in the JSON file store."""
    now = datetime.now().isoformat()
    try:
        # Load existing data
        data = {}
        if os.path.exists(STATUS_STORE_FILE):
            with open(STATUS_STORE_FILE, "r") as f:
                data = json.load(f)
        
        # Update or create entry
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
        
        # Save back to file
        os.makedirs(os.path.dirname(STATUS_STORE_FILE), exist_ok=True)
        with open(STATUS_STORE_FILE, "w") as f:
            json.dump(data, f, indent=2)
            
        logger.info(f"Call {call_id} status updated to: {status}")
    except Exception as e:
        logger.error(f"Failed to update call status file: {e}")


# TRUNK ID from env
OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN") 

# FastAPI app for triggering calls
app = FastAPI()

@app.post("/trigger-call")
async def trigger_call(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str = Header(None)
):
    """
    Webhook endpoint to trigger an outbound call.
    Validates API key and agent_id, then dispatches the call in the background.
    """
    # 1. Authentication
    api_key = os.getenv("API_KEY")
    if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ")[1] != api_key:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})

    # 2. Parse body
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid json"})

    phone = data.get("phone")
    if not phone:
        raise HTTPException(status_code=400, detail={"error": "phone is required"})

    # 3. Agent ID validation
    agent_id_env = os.getenv("AGENT_ID")
    agent_id_req = data.get("agent_id")
    if not agent_id_req:
        raise HTTPException(status_code=400, detail={"error": "agent_id is required"})
    if agent_id_req != agent_id_env:
        raise HTTPException(status_code=400, detail={"error": "invalid agent_id"})

    # 4. Background Task to trigger call - now synchronous to get the ID
    try:
        dispatch_id = await async_trigger_call(phone, data)
        # Initialize status as queued
        update_call_status(dispatch_id, "queued", phone)
        return {"status": "call queued", "call_id": dispatch_id}
    except Exception as e:
        logger.error(f"Failed to trigger call: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})

# Call status endpoint
# GET /call-status/{call_id}
# Same API key auth as /trigger-call
# Returns current status of a call by call_id
# Status values: queued, dialing, answered, completed, no-answer, failed
# Storage is in-memory JSON — resets on server restart, no DB needed
# call_id is returned when /trigger-call is called
@app.get("/call-status/{call_id}")
async def get_call_status(
    call_id: str,
    authorization: str = Header(None)
):
    """Retrieve the current status or full metrics of a call."""
    api_key = os.getenv("API_KEY")
    if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ")[1] != api_key:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})

    # 1. First, check if the full metrics file exists (for completed calls)
    metrics_file = os.path.join("KMS", "logs", f"call_{call_id}.json")
    try:
        if os.path.exists(metrics_file):
            with open(metrics_file, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading metrics file {metrics_file}: {e}")

    # 2. Fallback to the active calls store (for ongoing or queued calls)
    try:
        if os.path.exists(STATUS_STORE_FILE):
            with open(STATUS_STORE_FILE, "r") as f:
                data = json.load(f)
                status_data = data.get(call_id)
                if status_data:
                    return status_data
    except Exception as e:
        logger.error(f"Error reading status store: {e}")
        raise HTTPException(status_code=500, detail={"error": "could not read status"})

    raise HTTPException(status_code=404, detail={"error": "call not found"})





async def async_trigger_call(phone: str, variables: dict):
    """Asynchronously trigger the outbound call using LiveKit dispatch logic."""
    url = os.getenv("LIVEKIT_URL")
    lk_api_key = os.getenv("LIVEKIT_API_KEY")
    lk_api_secret = os.getenv("LIVEKIT_API_SECRET")
    agent_id = os.getenv("AGENT_ID")

    lk_api = api.LiveKitAPI(url=url, api_key=lk_api_key, api_secret=lk_api_secret)
    
    # Create a unique room for this call
    room_name = f"call-{phone.replace('+', '')}-{uuid.uuid4().hex[:4]}"
    
    # Prepare metadata with all variables
    metadata = variables.copy()
    metadata["phone_number"] = phone
    
    try:
        logger.info(f"Triggering outbound call to {phone}...")
        dispatch_request = api.CreateAgentDispatchRequest(
            agent_name=agent_id,
            room=room_name,
            metadata=json.dumps(metadata)
        )
        dispatch = await lk_api.agent_dispatch.create_dispatch(dispatch_request)
        
        # We need the dispatch ID to be in the metadata so the agent knows it
        # However, the dispatch is already created. We can update metadata if needed, 
        # but simpler is to just pass it in the INITIAL metadata if we knew it.
        # Since we don't, we'll just return it here for the HTTP response.
        # The agent can also find its dispatch ID from ctx.job.dispatch_id.
        
        logger.info(f"✅ Call Dispatched Successfully to {phone}! ID: {dispatch.id}")
        return dispatch.id
    except Exception as e:
        logger.error(f"❌ Error dispatching call: {e}")
        raise e
    finally:
        await lk_api.aclose()



def _build_tts():
    """Configure the Text-to-Speech provider based on env vars."""
    provider = os.getenv("TTS_PROVIDER", "openai").lower()
    
    if provider == "cartesia":
        logger.info("Using Cartesia TTS")
        model = os.getenv("CARTESIA_TTS_MODEL", "sonic-2")
        voice_id = os.getenv("CARTESIA_TTS_VOICE", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
        return cartesia.TTS(model=model, voice=voice_id)
    
    if provider == "sarvam":
        logger.info("Using Sarvam TTS")
        model = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
        language = os.getenv("SARVAM_TARGET_LANGUAGE_CODE", "hi-IN")
        api_key = os.getenv("SARVAM_API_KEY")
        return sarvam.TTS(model=model, target_language_code=language, api_key=api_key)
    
    # Default to OpenAI
    logger.info("Using OpenAI TTS")
    model = os.getenv("OPENAI_TTS_MODEL", "tts-1")
    voice_name = os.getenv("OPENAI_TTS_VOICE", "alloy")
    return openai.TTS(model=model, voice=voice_name)


def _build_llm():
    """Configure the LLM provider based on env vars."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    
    if provider == "groq":
        logger.info("Using Groq LLM")
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        api_key = os.getenv("GROQ_API_KEY")
        return groq.LLM(model=model, api_key=api_key)
    
    # Default to OpenAI
    logger.info("Using OpenAI LLM")
    model = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
    return openai.LLM(model=model)


class TransferFunctions(llm.ToolContext):
    def __init__(self, ctx: agents.JobContext, phone_number: str = None):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number

    @llm.function_tool(description="Transfer the call to a human support agent or another phone number.")
    async def transfer_call(self, destination: Optional[str] = None):
        """Transfer the call."""
        if destination is None:
            destination = os.getenv("DEFAULT_TRANSFER_NUMBER")
            if not destination:
                 return "Error: No default transfer number configured."
        if "@" not in destination:
            if SIP_DOMAIN:
                clean_dest = destination.replace("tel:", "").replace("sip:", "")
                destination = f"sip:{clean_dest}@{SIP_DOMAIN}"
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
            logger.error("Could not determine participant identity for transfer")
            return "Failed to transfer: could not identify the caller."

        try:
            logger.info(f"Transferring participant {participant_identity} to {destination}")
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


class OutboundAssistant(Agent):
    """An AI agent tailored for outbound calls."""
    def __init__(self) -> None:
        super().__init__(
            instructions="""
            You are a helpful and professional voice assistant calling from Vobiz.
            
            Key behaviors:
            1. Introduce yourself clearly when the user answers.
            2. Be concise and respect the user's time.
            3. If asked, explain you are an AI assistant helping with a test call.
            4. If the user asks to be transferred, call the transfer_call tool immediately.
               If no number is specified, do NOT ask for one; just call the tool with the default.
            """
        )


async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint for the agent."""
    logger.info(f"Connecting to room: {ctx.room.name}")
    
    phone_number = None
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
    except Exception:
        logger.warning("No valid JSON metadata found. This might be an inbound call.")

    fnc_ctx = TransferFunctions(ctx, phone_number)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=_build_llm(),
        tts=_build_tts(),
        tools=fnc_ctx._tools,
    )

    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )


    # Initialize Metrics Tracking
    # Use dispatch_id as call_id for synchronization if available
    call_id = ctx.job.dispatch_id or ctx.room.sid
    if asyncio.iscoroutine(call_id):
        call_id = await call_id



    room_name = ctx.room.name
    if asyncio.iscoroutine(room_name):
        room_name = await room_name

    metrics = CallMetrics(
        call_id=call_id,
        conversation_id=room_name,
        direction="outbound" if phone_number else "inbound",
        to_number=phone_number,
        from_number=os.getenv("VOBIZ_OUTBOUND_NUMBER"),
        stt_provider="deepgram",
        tts_provider=os.getenv("TTS_PROVIDER", "openai"),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        call_start_time=datetime.now().isoformat(),
        call_status="active"
    )
    update_call_status(call_id, "active", phone_number)


    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev: voice.UserInputTranscribedEvent):
        if ev.is_final:
            text = ev.transcript
            metrics.transcript_segments.append(TranscriptSegment(
                text=text,
                speaker="user",
                timestamp=time.time()
            ))
            metrics.transcript += f"User: {text}\n"

    @session.on("conversation_item_added")
    def on_conversation_item_added(ev: voice.ConversationItemAddedEvent):
        if hasattr(ev.item, "role") and ev.item.role == "assistant" and ev.item.content:
            text = " ".join(ev.item.content) if isinstance(ev.item.content, list) else str(ev.item.content)
            metrics.transcript_segments.append(TranscriptSegment(
                text=text,
                speaker="agent",
                timestamp=time.time()
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
        update_call_status(call_id, "failed")


    async def save_metrics():
        logger.info(f"Saving call metrics for {ctx.room.name}...")
        metrics.call_end_time = datetime.now().isoformat()
        if metrics.call_status == "active": # Only update if not already set to failed/no-answer
            metrics.call_status = "completed"
            update_call_status(call_id, "completed")
        try:
            start = datetime.fromisoformat(metrics.call_start_time)

            end = datetime.fromisoformat(metrics.call_end_time)
            metrics.call_duration = (end - start).total_seconds()
        except:
            pass
        path = storage.save(metrics)
        logger.info(f"Metrics saved to {path}")

    ctx.add_shutdown_callback(save_metrics)

    if phone_number:
        logger.info(f"Initiating outbound SIP call to {phone_number}...")
        update_call_status(call_id, "dialing", phone_number)
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=OUTBOUND_TRUNK_ID,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered! Agent is now listening.")
            update_call_status(call_id, "answered", phone_number)
        except Exception as e:
            logger.error(f"Failed to place outbound call: {e}")
            error_status = "failed"
            if "timeout" in str(e).lower() or "no-answer" in str(e).lower():
                error_status = "no-answer"
            update_call_status(call_id, error_status, phone_number)
            ctx.shutdown()

    else:
        logger.info("No phone number in metadata. Treating as inbound/web call.")
        await session.generate_reply(instructions="Greet the user.")


def run_fastapi():
    """Run the FastAPI server using uvicorn."""
    try:
        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        port_str = base_url.split(":")[-1].split("/")[0]
        port = int(port_str)
        logger.info(f"Starting trigger API on port {port}...")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="error")
    except Exception as e:
        logger.error(f"Failed to start FastAPI server: {e}")

if __name__ == "__main__":
    # Start FastAPI in a background thread to ensure it shares status file with the worker
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Run the LiveKit Agent worker using the standard CLI
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller", 
        )
    )
