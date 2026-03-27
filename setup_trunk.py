import asyncio
import os
from dotenv import load_dotenv, set_key
from livekit import api

# Load environment variables
load_dotenv(".env")

async def main():
    # Initialize LiveKit API
    lkapi = api.LiveKitAPI()
    sip = lkapi.sip
    
    address = os.getenv("VOBIZ_SIP_DOMAIN")
    username = os.getenv("VOBIZ_USERNAME")
    password = os.getenv("VOBIZ_PASSWORD")
    number = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    if not all([address, username, password, number]):
        print("Error: Missing VOBIZ_SIP_DOMAIN, VOBIZ_USERNAME, VOBIZ_PASSWORD, or VOBIZ_OUTBOUND_NUMBER in .env")
        return

    print("Checking existing SIP trunks...")
    
    # Check for existing outbound trunk
    ob_trunks = await sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    ob_trunk_id = None
    for t in ob_trunks.items:
        if t.address == address and t.auth_username == username:
            ob_trunk_id = t.sip_trunk_id
            print(f"ℹ️ Found existing Outbound Trunk: {ob_trunk_id}")
            break
            
    if not ob_trunk_id:
        print("Creating new SIP Outbound Trunk...")
        ob_req = api.CreateSIPOutboundTrunkRequest(
            trunk=api.SIPOutboundTrunkInfo(
                name="Vobiz Outbound Trunk",
                address=address,
                numbers=[number],
                auth_username=username,
                auth_password=password,
            )
        )
        ob_trunk = await sip.create_outbound_trunk(ob_req)
        ob_trunk_id = ob_trunk.sip_trunk_id
        print(f"✅ Created Outbound Trunk: {ob_trunk_id}")
        
        # Save to .env
        env_file = ".env"
        set_key(env_file, "OUTBOUND_TRUNK_ID", ob_trunk_id)
        print("📝 Updated OUTBOUND_TRUNK_ID in .env")


    # Check for existing inbound trunk
    in_trunks = await sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    in_trunk_id = None
    for t in in_trunks.items:
        if number in t.numbers:
            in_trunk_id = t.sip_trunk_id
            print(f"ℹ️ Found existing Inbound Trunk: {in_trunk_id}")
            break
            
    if not in_trunk_id:
        print("Creating new SIP Inbound Trunk...")
        in_req = api.CreateSIPInboundTrunkRequest(
            trunk=api.SIPInboundTrunkInfo(
                name="Vobiz Inbound Trunk",
                numbers=[number],
                allowed_addresses=[address],
            )
        )
        in_trunk = await sip.create_inbound_trunk(in_req)
        in_trunk_id = in_trunk.sip_trunk_id
        print(f"✅ Created Inbound Trunk: {in_trunk_id}")

    await lkapi.aclose()
    print("\n✅ SIP Trunks setup complete! You can now run create_dispatch_rule.py")

if __name__ == "__main__":
    asyncio.run(main())
