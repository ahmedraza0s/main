import subprocess
import sys
import time

def main():
    print("====================================")
    print("Starting Vobiz Agent System...")
    print("====================================")
    
    commands = [
        ("Outbound Agent & API", [sys.executable, "outbound_agent.py", "start"]),
        ("Inbound Agent", [sys.executable, "inbound_agent.py", "start"])
    ]
    
    processes = []
    
    for name, cmd in commands:
        print(f"[{name}] Starting...")
        # Start process and pipe output to terminal
        proc = subprocess.Popen(cmd)
        processes.append((name, proc))
        time.sleep(1) # Slight stagger for cleaner logs
        
    print("\n✅ All services started successfully. Press Ctrl+C to stop.")
    
    try:
        # Keep main process alive while children run
        for _, proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down all services...")
        for name, proc in processes:
            print(f"[{name}] Terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Shutdown complete.")

if __name__ == "__main__":
    main()
