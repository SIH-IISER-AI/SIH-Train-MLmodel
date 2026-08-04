import time
from contracts import SimulationTick, TrainTelemetry
from redis_client import RedisStreamBridge

def main():
    print("Initializing Simulator...")
    redis_bridge = RedisStreamBridge()
    tick_counter = 0

    while True:
        try:
            # 1. The Data Team writes the logic to generate this data
            current_time = int(time.time() * 1000)
            tick_counter += 1
            
            # 2. Enforce the contract
            tick_event = SimulationTick(
                timestamp=current_time,
                tick_id=tick_counter,
                time_multiplier=1,
                active_train_count=1, # Fake data for now
                network_health_score=100.0
            )
            
            # 3. Publish to Redis
            redis_bridge.publish_event(tick_event)
            print(f"Published Tick: {tick_counter}")
            
            # Simulate the loop delay (e.g., 2 seconds per tick)
            time.sleep(2)
            
        except KeyboardInterrupt:
            print("Simulator stopped.")
            break
        except Exception as e:
            print(f"Simulation crashed: {e}")
            break

if __name__ == "__main__":
    main()