import logging
import os
import json
import asyncio
from datetime import datetime

from dotenv import load_dotenv
from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions, voice
from livekit.plugins import deepgram, noise_cancellation, silero

from storage import CallMetrics
from shared_configs import (
    _build_tts, _build_llm, TransferFunctions, 
    update_call_status, bind_metrics_events, finalize_metrics
)

# Load environment variables
load_dotenv(".env")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inbound-agent")

class InboundAssistant(Agent):
    """An AI agent tailored for inbound calls."""
    def __init__(self) -> None:
        super().__init__(
            instructions="""
            You are a helpful and professional voice assistant calling from Vobiz Your name is inbound assistant..
            
            Key behaviors:
            1. Greet the user warmly when they join the call.
            2. Be concise and help them with their inquiry.
            3. If asked, explain you are an AI assistant.
            4. If the user asks to be transferred, call the transfer_call tool.
            """
        )

async def inbound_entrypoint(ctx: agents.JobContext):
    logger.info(f"Connecting to room: {ctx.room.name} (Inbound)")
    
    # Inbound calls usually don't have phone_number in metadata immediately
    # Unless passed via SIP headers mapped to metadata
    phone_number = None
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
    except Exception:
        pass

    fnc_ctx = TransferFunctions(ctx, phone_number)
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=_build_llm(),
        tts=_build_tts(),
        vad=silero.VAD.load(min_silence_duration=0.5),
        tools=fnc_ctx._tools,
    )

    await session.start(
        room=ctx.room,
        agent=InboundAssistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )

    call_id = ctx.job.dispatch_id or ctx.room.sid
    if asyncio.iscoroutine(call_id):
        call_id = await call_id

    metrics = CallMetrics(
        call_id=call_id,
        conversation_id=ctx.room.name,
        direction="inbound",
        to_number=os.getenv("VOBIZ_OUTBOUND_NUMBER"), # The agent is the 'to' in inbound
        from_number=phone_number,
        call_start_time=datetime.now().isoformat(),
        call_status="active"
    )
    
    await update_call_status(call_id, "active", phone_number)
    bind_metrics_events(session, metrics, call_id)
    ctx.add_shutdown_callback(lambda: finalize_metrics(ctx, metrics, call_id))

    # Greet the user immediately on inbound
    await session.generate_reply(instructions="Hello! How can I assist you today?")

async def inbound_request_fnc(req: agents.JobRequest) -> None:
    """Only accept calls routed via the inbound SIP dispatch rule."""
    if req.job.room.name.startswith("inbound-"):
        await req.accept()
    else:
        await req.reject()

if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=inbound_entrypoint,
            agent_name=os.getenv("INBOUND_AGENT_ID", "inbound-caller"),
            request_fnc=inbound_request_fnc,
            port=0,
        )
    )
