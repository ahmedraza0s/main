from fastapi import FastAPI
import uvicorn
import os
from dotenv import load_dotenv

load_dotenv(".env")

app = FastAPI()

@app.get("/")
def read_root():
    return {"Hello": "World"}

if __name__ == "__main__":
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    port = int(base_url.split(":")[-1].split("/")[0])
    print(f"Starting test server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
