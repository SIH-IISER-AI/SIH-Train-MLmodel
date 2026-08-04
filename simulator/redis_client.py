import json
import redis
from pydantic import BaseModel

class RedisStreamBridge:
    def __init__(self, host='localhost', port=6379, stream_name='telemetry_stream'):
        # decode_responses=True prevents b'byte' string headaches later
        self.client = redis.Redis(host=host, port=port, decode_responses=True)
        self.stream_name = stream_name

    def publish_event(self, event_model: BaseModel):
        """
        Takes a strictly typed Pydantic model, serializes it, 
        and pushes it to the Redis Stream.
        """
        try:
            # Convert Pydantic model to JSON string
            payload_json = event_model.model_dump_json()
            
            # XADD pushes to a stream. We store the JSON under the key 'payload'
            message_id = self.client.xadd(
                self.stream_name, 
                {"payload": payload_json}
            )
            return message_id
        except Exception as e:
            print(f"CRITICAL: Failed to push to Redis: {e}")
            raise