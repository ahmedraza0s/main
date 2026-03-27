import asyncio
import os
from dotenv import load_dotenv
from livekit import api

# Load environment variables
load_dotenv(".env")

async def main():
    # 1. Initialize LiveKit API
    lk_api = api.LiveKitAPI()
    
    AGENT_NAME = os.getenv("INBOUND_AGENT_ID", "inbound-caller")
    
    print(f"Setting up Dispatch Rule for Agent: '{AGENT_NAME}'")

    try:
        # 2. Get the Inbound Trunk that we created in setup_trunk.py
        in_trunks = await lk_api.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        if not in_trunks.items:
            print("❌ No Inbound trunk found. Please run 'python setup_trunk.py' first.")
            return

        # Use the first inbound trunk's ID, or all of them
        trunk_ids = [t.sip_trunk_id for t in in_trunks.items]
        print(f"Found Inbound Trunks: {trunk_ids}")

        # 3. Check if a rule for this agent already exists
        existing_rules = await lk_api.sip.list_sip_dispatch_rule(
            api.ListSIPDispatchRuleRequest()
        )
        
        for rule_info in existing_rules.items:
            rule = rule_info.rule
            existing_prefix = None
            
            try:
                # Safely get the room prefix based on what field is set
                rule_type = rule.WhichOneof('rule')
                if rule_type:
                    rule_obj = getattr(rule, rule_type, None)
                    if rule_obj and hasattr(rule_obj, 'room_prefix'):
                        existing_prefix = rule_obj.room_prefix
            except Exception as e:
                print(f"Skipping rule due to parsing error: {e}")
            
            if existing_prefix == "inbound-":
                print(f"\nℹ️  A dispatch rule for prefix 'inbound-' already exists.")
                print(f"Existing Rule ID: {rule_info.sip_dispatch_rule_id}")
                return

        # 4. Create a SIP Dispatch Rule if not found
        dispatch_rule = api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                room_prefix="inbound-"
            )
        )
        
        rule_info = await lk_api.sip.create_sip_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                rule=dispatch_rule,
                trunk_ids=trunk_ids,
                hide_phone_number=False,
                name=f"Rule for {AGENT_NAME}"
            )
        )

        print("\n✅ Dispatch Rule Created Successfully!")
        print(f"Rule ID: {rule_info.sip_dispatch_rule_id}")
        print(f"Description: Routes calls from trunks {trunk_ids} to rooms prefixed with 'inbound-'")
        
    except Exception as e:
        print(f"\n❌ Failed to create dispatch rule: {e}")
    
    finally:
        await lk_api.aclose()

if __name__ == "__main__":
    asyncio.run(main())
