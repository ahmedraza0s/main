import json
import os
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class TranscriptSegment(BaseModel):
    text: str
    speaker: str # 'agent' or 'user'
    timestamp: float

class CallMetrics(BaseModel):
    # Identifiers
    call_id: Optional[str] = None
    provider_call_id: Optional[str] = None
    conversation_id: Optional[str] = None
    direction: str = "outbound"

    # Numbers
    caller_number: Optional[str] = None
    receiver_number: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    sip_number: Optional[str] = None
    sip_call_id: Optional[str] = None
    sip_provider: str = "vobiz"

    # Timestamps
    call_start_time: Optional[str] = None
    call_answer_time: Optional[str] = None
    call_end_time: Optional[str] = None
    call_duration: float = 0.0

    # Status
    call_status: str = "pending"

    # Transcripts
    transcript: str = ""
    transcript_segments: List[TranscriptSegment] = []

    # Intelligence
    intent_detected: Optional[str] = None
    sentiment: Optional[str] = None

    # AI Info
    ai_model_used: Optional[str] = None
    ai_prompt: Optional[str] = None
    ai_response: Optional[str] = None

    # Providers
    stt_provider: str = "deepgram"
    tts_provider: str = "sarvam"
    llm_provider: str = "groq"

    # Latencies (in ms)
    stt_latency: List[float] = [] # list of latencies for each speech segment
    llm_latency: List[float] = []
    tts_latency: List[float] = []
    total_response_time: Optional[float] = None

    # Performance Metadata
    jitter: Optional[float] = None
    packet_loss: Optional[float] = None
    mos_score: Optional[float] = None

    # Outcomes
    call_outcome: Optional[str] = None
    tags: List[str] = []
    notes: Optional[str] = None

    # Customer/Agent IDs
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: str = "outbound-caller"

    # Costs (placeholder for calculation)
    sip_cost: float = 0.0
    stt_cost: float = 0.0
    tts_cost: float = 0.0
    llm_cost: float = 0.0
    total_call_cost: float = 0.0

    # System Info
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

class BaseStorage:
    def save(self, metrics: CallMetrics):
        raise NotImplementedError

class JsonFileStorage(BaseStorage):
    def __init__(self, log_dir: str = "KMS/logs"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

    def save(self, metrics: CallMetrics):
        metrics.updated_at = datetime.now().isoformat()
        filename = f"call_{metrics.call_id or metrics.conversation_id or int(time.time())}.json"
        filepath = os.path.join(self.log_dir, filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(metrics.model_dump_json(indent=2))
        return filepath
