import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import redis.asyncio as redis

app = FastAPI()

# Connect to the Redis container
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # Track the last ID read from the stream to ensure no dropped ticks
    last_id = "0-0" 
    
    try:
        while True:
            # Block for up to 1 second waiting for new messages in 'telemetry_stream'
            messages = await redis_client.xread(
                {"telemetry_stream": last_id}, 
                count=10, 
                block=1000
            )
            
            if messages:
                for stream_name, stream_messages in messages:
                    for message_id, message_data in stream_messages:
                        # Forward the raw JSON to the Next.js UI
                        await websocket.send_json(message_data)
                        last_id = message_id
            
            # Allow the event loop to breathe
            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        print("Client disconnected from dashboard")
    except Exception as e:
        print(f"Error: {e}")