import requests
import json
import os
import time
from dotenv import load_dotenv

load_dotenv(".env")

def test_full_lifecycle():
    api_key = os.getenv("API_KEY")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    agent_id = os.getenv("OUTBOUND_AGENT_ID", "outbound-caller")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 1. Trigger Call
    trigger_url = f"{base_url}/trigger-call"
    payload = {
        "phone": "+918046733439", # Replace with real number for full test, or keep for status test
        "agent_id": agent_id
    }
    
    print(f"1. Triggering call to {payload['phone']}...")
    try:
        resp = requests.post(trigger_url, headers=headers, json=payload)
        if resp.status_code != 200:
            print(f"Failed to trigger: {resp.text}")
            return
        
        call_id = resp.json().get("call_id")
        print(f"✅ Triggered! Call ID: {call_id}")
        
        # 2. Poll Status
        status_url = f"{base_url}/call-status/{call_id}"
        print(f"\n2. Polling status for {call_id}...")
        
        for i in range(10): # Poll for ~20 seconds
            status_resp = requests.get(status_url, headers=headers)
            if status_resp.status_code == 200:
                data = status_resp.json()
                print(f"[{i}] Status: {data['status']} | Updated: {data['updated_at']}")
                if data['status'] in ['completed', 'failed', 'no-answer']:
                    print("\n✅ Final Status Reached!")
                    break
            else:
                print(f"Error fetching status: {status_resp.text}")
            
            time.sleep(2)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_full_lifecycle()
