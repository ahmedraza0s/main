import logging
import os
import json
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import (
    openai,
    cartesia,
    deepgram,
    noise_cancellation,
    silero,
    sarvam,
    groq,
)
from livekit.agents import llm, stt, tts, voice
from typing import Annotated, Optional
from datetime import datetime
import time
import asyncio
from storage import CallMetrics, JsonFileStorage, TranscriptSegment

# Load environment variables
load_dotenv(".env")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# Initialize storage
storage = JsonFileStorage()


# TRUNK ID - This needs to be set after you crate your trunk
# You can find this by running 'python setup_trunk.py --list' or checking LiveKit Dashboard
OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN") 


def _build_tts():
    """Configure the Text-to-Speech provider based on env vars."""
    provider = os.getenv("TTS_PROVIDER", "openai").lower()
    
    if provider == "cartesia":
        logger.info("Using Cartesia TTS")
        model = os.getenv("CARTESIA_TTS_MODEL", "sonic-2")
        voice = os.getenv("CARTESIA_TTS_VOICE", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
        return cartesia.TTS(model=model, voice=voice)
    
    if provider == "sarvam":
        logger.info("Using Sarvam TTS")
        model = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
        language = os.getenv("SARVAM_TARGET_LANGUAGE_CODE", "hi-IN")
        api_key = os.getenv("SARVAM_API_KEY")
        return sarvam.TTS(model=model, target_language_code=language, api_key=api_key)
    
    # Default to OpenAI
    logger.info("Using OpenAI TTS")
    model = os.getenv("OPENAI_TTS_MODEL", "tts-1")
    voice = os.getenv("OPENAI_TTS_VOICE", "alloy")
    return openai.TTS(model=model, voice=voice)


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
        """
        Transfer the call.
        """
        if destination is None:
            destination = os.getenv("DEFAULT_TRANSFER_NUMBER")
            if not destination:
                 return "Error: No default transfer number configured."
        if "@" not in destination:
            # If no domain is provided, append the SIP domain
            if SIP_DOMAIN:
                # Ensure clean number (strip tel: or sip: prefix if present but no domain)
                clean_dest = destination.replace("tel:", "").replace("sip:", "")
                destination = f"sip:{clean_dest}@{SIP_DOMAIN}"
            else:
                # Fallback to tel URI if no domain configured
                if not destination.startswith("tel:") and not destination.startswith("sip:"):
                     destination = f"tel:{destination}"
        elif not destination.startswith("sip:"):
             destination = f"sip:{destination}"
        
        logger.info(f"Transferring call to {destination}")
        
        # Determine the participant identity
        # For outbound calls initiated by this agent, the participant identity is typically "sip_<phone_number>"
        # For inbound, we might need to find the remote participant.
        participant_identity = None
        
        # If we stored the phone number from metadata, we can construct the identity
        if self.phone_number:
            participant_identity = f"sip_{self.phone_number}"
        else:
            # Try to find a participant that is NOT the agent
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

    """
    An AI agent tailored for outbound calls.
    Attempts to be helpful and concise.
    """
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
    """
    Main entrypoint for the agent.
    
    For outbound calls:
    1. Checks for 'phone_number' in the job metadata.
    2. Connects to the room.
    3. Initiates the SIP call to the phone number.
    4. Waits for answer before speaking.
    """
    logger.info(f"Connecting to room: {ctx.room.name}")
    
    # parse the phone number from the metadata sent by the dispatch script
    phone_number = None
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
    except Exception:
        logger.warning("No valid JSON metadata found. This might be an inbound call.")

    # Initialize function context
    fnc_ctx = TransferFunctions(ctx, phone_number)

    # Initialize the Agent Session with plugins

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=_build_llm(),
        tts=_build_tts(),
        tools=fnc_ctx._tools,
    )

    # Start the session
    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True, # Close room when agent disconnects
        ),
    )

    # Initialize Metrics Tracking
    room_sid = ctx.room.sid
    if asyncio.iscoroutine(room_sid):
        room_sid = await room_sid
    
    room_name = ctx.room.name
    if asyncio.iscoroutine(room_name):
        room_name = await room_name

    metrics = CallMetrics(
        call_id=room_sid,
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

    # Event Listeners for Data Capture
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
        # Capture assistant responses
        if hasattr(ev.item, "role") and ev.item.role == "assistant" and ev.item.content:
            content = ev.item.content
            if isinstance(content, list):
                text = " ".join(content)
            else:
                text = str(content)

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

    # Shutdown Callback to Save Data
    async def save_metrics():
        logger.info(f"Saving call metrics for {ctx.room.name}...")
        metrics.call_end_time = datetime.now().isoformat()
        metrics.call_status = "completed"
        # Calculate duration if possible
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
        try:
            # Create a SIP participant to dial out
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
        except Exception as e:
            logger.error(f"Failed to place outbound call: {e}")
            ctx.shutdown()
    else:
        logger.info("No phone number in metadata. Treating as inbound/web call.")
        await session.generate_reply(instructions="Greet the user.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller", 
        )
    )
