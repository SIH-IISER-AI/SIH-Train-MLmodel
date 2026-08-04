import time
import redis
from contracts import TrainTelemetry, ConflictAlert, DispatchRecommendation

def main():
    print("Initializing AI Decision Engine...")
    client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    
    # Start reading from the latest message in the stream
    last_id = '$' 
    
    while True:
        try:
            # Block and wait for new telemetry
            messages = client.xread({"telemetry_stream": last_id}, count=10, block=2000)
            
            if not messages:
                continue

            for stream_name, stream_messages in messages:
                for message_id, message_data in stream_messages:
                    last_id = message_id
                    raw_payload = message_data.get("payload")
                    
                    if not raw_payload:
                        continue
                    
                    # 1. PARSE: Let it crash if the contract is violated
                    if "TRAIN_TELEMETRY" in raw_payload:
                        telemetry = TrainTelemetry.model_validate_json(raw_payload)
                        
                        # ---------------------------------------------------------
                        # [ML TEAM]: INSERT GOOGLE OR-TOOLS LOGIC HERE.
                        # Track ETAs, calculate block overlaps, build the matrix.
                        # ---------------------------------------------------------
                        conflict_detected = False # Replace with actual logic
                        
                        if conflict_detected:
                            print(f"Conflict detected involving {telemetry.train_id}!")
                            
                            # 2. EMIT: Push the alert and recommendation to a NEW stream
                            # alert_payload = ConflictAlert(...).model_dump_json()
                            # client.xadd("decision_stream", {"payload": alert_payload})
                            pass 

        except KeyboardInterrupt:
            print("AI Engine shutting down.")
            break
        except Exception as e:
            print(f"AI Engine Error: {e}")
            time.sleep(1) # Prevent tight crash loops

if __name__ == "__main__":
    main()