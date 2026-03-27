import os
import json
from storage import CallMetrics, JsonFileStorage, TranscriptSegment

def test_json_storage():
    # Setup
    test_dir = "KMS/test_logs"
    storage = JsonFileStorage(log_dir=test_dir)
    
    # Create mock metrics
    metrics = CallMetrics(
        call_id="test_call_123",
        conversation_id="test_conv_456",
        direction="outbound",
        to_number="+918046733439",
        transcript="User: Hello\nAgent: Hi there!\n",
        transcript_segments=[
            TranscriptSegment(text="Hello", speaker="user", timestamp=100.0),
            TranscriptSegment(text="Hi there!", speaker="agent", timestamp=102.0)
        ],
        stt_latency=[150.0, 200.0],
        llm_latency=[500.0],
        tts_latency=[300.0],
        call_status="completed"
    )
    
    # Save
    path = storage.save(metrics)
    print(f"Saved to {path}")
    
    # Verify file exists
    assert os.path.exists(path)
    
    # Verify content
    with open(path, "r") as f:
        data = json.load(f)
        assert data["call_id"] == "test_call_123"
        assert data["direction"] == "outbound"
        assert len(data["transcript_segments"]) == 2
        assert data["stt_latency"] == [150.0, 200.0]
        assert "created_at" in data
        assert "updated_at" in data

    print("✅ Unit test passed!")

if __name__ == "__main__":
    test_json_storage()
