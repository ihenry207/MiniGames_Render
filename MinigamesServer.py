from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import json
import socket  
import random
import csv
import os
from datetime import datetime

app = FastAPI()

# --- CSV LOGGING SETUP ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

def log_game_event(game_name: str, device_id: str, event_type: str, level: str = "N/A", status: str = "N/A", details: str = ""):
    filename = os.path.join(DATA_DIR, f"{game_name}.csv")
    file_exists = os.path.isfile(filename)
    with open(filename, mode='a', newline='') as csvfile:
        fieldnames = ['Timestamp', 'Device ID', 'Event Type', 'Level Reached', 'Status', 'Details']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Device ID': device_id,
            'Event Type': event_type,
            'Level Reached': level,
            'Status': status,
            'Details': details
        })

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[device_id] = websocket
        print(f"[SERVER] [+] Node Connected: {device_id}")

    def disconnect(self, device_id: str):
        if device_id in self.active_connections:
            del self.active_connections[device_id]
            print(f"[SERVER] [-] Node Disconnected: {device_id}")

manager = ConnectionManager()

# --- GAME STATES ---
game_states = {} 

wavelength_state = {
    "status": "lobby", 
    "registered_players": [], 
    "host_index": 0, 
    "word_options": [
        "1: Hot - Cold",
        "2: Useless - Useful",
        "3: Soft - Hard",
        "4: Trashy - Classy",
        "5: Boring - Exciting"
    ],
    "current_word": "",
    "target_score": 0,
    "guesses": {} 
}

@app.websocket("/ws/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str):
    await manager.connect(device_id, websocket)
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            
            # ---------------------------------------------------------
            # 1. INITIAL GAME SELECTION (Polling Mechanism)
            # ---------------------------------------------------------
            if msg_type == "GAME_SELECT":
                game_selected = message.get("game")
                
                # --- MEMORY GAME LOGIC ---
                if game_selected == "led_memory":
                    base_pattern = [random.choice(["red", "green", "yellow"]) for _ in range(10)]
                    game_states[device_id] = base_pattern 
                    patterns = [base_pattern[:i+1] for i in range(10)]
                    
                    log_game_event("led_memory", device_id, "GAME_START", level="1-10", status="PENDING")
                    
                    await websocket.send_text(json.dumps({
                        "type": "PATTERN", "patterns": patterns, "start_level": 1
                    }))
                
                # --- WAVELENGTH GAME LOGIC ---
                elif game_selected == "wavelength":
                    if device_id not in wavelength_state["registered_players"]:
                        wavelength_state["registered_players"].append(device_id)
                        print(f"[SERVER] {device_id} joined Wavelength. Total players: {len(wavelength_state['registered_players'])}")
                        
                        # +++ LOGGING: Player Joined Lobby +++
                        log_game_event("wavelength", device_id, "JOIN_LOBBY", status="CONNECTED")

                    current_host_id = wavelength_state["registered_players"][wavelength_state["host_index"]]

                    if device_id == current_host_id:
                        wavelength_state["status"] = "host_choosing"
                        
                        # +++ LOGGING: Assigned as Host +++
                        log_game_event("wavelength", device_id, "ROLE_ASSIGNED", status="HOST")
                        
                        await websocket.send_text(json.dumps({
                            "type": "WAVELENGTH_ROLE",
                            "role": "host",
                            "words": wavelength_state["word_options"]
                        }))
                        print(f"[SERVER] Sent Host options to {device_id}.")
                    
                    else:
                        if wavelength_state["status"] == "guessing":
                            await websocket.send_text(json.dumps({
                                "type": "WAVELENGTH_ROLE",
                                "role": "player_guess",
                                "word": wavelength_state["current_word"]
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "WAVELENGTH_ROLE",
                                "role": "player_wait"
                            }))

            # ---------------------------------------------------------
            # 2. WAVELENGTH HOST UPLOADS TARGET
            # ---------------------------------------------------------
            elif msg_type == "HOST_SUBMIT":
                word_index = message.get("word_index") 
                target_score = message.get("score")
                selected_word = wavelength_state["word_options"][word_index - 1]
                
                wavelength_state["current_word"] = selected_word
                wavelength_state["target_score"] = target_score
                wavelength_state["status"] = "guessing"
                wavelength_state["guesses"] = {} 
                
                print(f"\n[SERVER] HOST LOCKED IN! Word: '{selected_word}' | Target: {target_score}%")
                
                # +++ LOGGING: Host locked in word and target +++
                log_game_event("wavelength", device_id, "HOST_LOCKED_IN", level=selected_word, status="PENDING_GUESSES", details=f"Target: {target_score}%")

            # ---------------------------------------------------------
            # 3. WAVELENGTH PLAYER UPLOADS GUESS
            # ---------------------------------------------------------
            elif msg_type == "PLAYER_GUESS":
                guess_score = message.get("score")
                wavelength_state["guesses"][device_id] = guess_score
                print(f"[SERVER] Received guess from {device_id}: {guess_score}%")
                
                # +++ LOGGING: Player locked in their guess +++
                log_game_event("wavelength", device_id, "PLAYER_GUESSED", level=wavelength_state["current_word"], status="GUESSED", details=f"Guess: {guess_score}%")
                
                total_players = len(wavelength_state["registered_players"])
                if len(wavelength_state["guesses"]) >= (total_players - 1) and total_players > 1:
                    print("\n[SERVER] ALL GUESSES IN! Round Complete.")
                    
                    # +++ LOGGING: Final Round Results +++
                    log_game_event("wavelength", "System", "ROUND_END", level=wavelength_state["current_word"], status="COMPLETED", details=f"Target: {wavelength_state['target_score']}%, Guesses: {wavelength_state['guesses']}")
                    
                    # ROUND ROBIN ROTATION
                    wavelength_state["host_index"] = (wavelength_state["host_index"] + 1) % total_players
                    wavelength_state["status"] = "lobby"
                    wavelength_state["current_word"] = ""
                    wavelength_state["target_score"] = 0
                    wavelength_state["guesses"] = {}
                    
                    print(f"[SERVER] Round reset. Next host is: {wavelength_state['registered_players'][wavelength_state['host_index']]}\n")

            # ---------------------------------------------------------
            # 4. MEMORY GAME RESULTS
            # ---------------------------------------------------------
            elif msg_type == "GAME_RESULTS":
                results = message.get("results", [])
                device = message.get("device_id")
                
                print(f"\n[SERVER] [RESULTS] Received batch from {device}: {results}")
                
                if "loss" in results:
                    total_levels = len(game_states.get(device, [])) - 10 + len(results)
                    log_game_event("led_memory", device, "GAME_OVER", level=str(total_levels), status="LOSS", details=str(results))
                    if device in game_states:
                        del game_states[device]
                else:
                    old_pattern = game_states.get(device, [])
                    log_game_event("led_memory", device, "BATCH_WIN", level=str(len(old_pattern)), status="WIN", details=str(results))
                    
                    new_additions = [random.choice(["red", "green", "yellow"]) for _ in range(10)]
                    new_base_pattern = old_pattern + new_additions
                    game_states[device] = new_base_pattern 
                    
                    start_level = len(old_pattern) + 1
                    patterns = [new_base_pattern[:i+1] for i in range(len(old_pattern), len(new_base_pattern))]
                    
                    log_game_event("led_memory", device, "NEXT_BATCH_SENT", level=f"{start_level}-{start_level+9}", status="PENDING")
                    
                    await websocket.send_text(json.dumps({
                        "type": "PATTERN", "patterns": patterns, "start_level": start_level
                    }))

    except WebSocketDisconnect:
        manager.disconnect(device_id)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

if __name__ == "__main__":
    import uvicorn
    import os

    # Render provides a specific port. If not on Render, default to 8000.
    port = int(os.environ.get("PORT", 8000)) 

    print("\n" + "="*55)
    print(f"  MINIGAMES HOST SERVER INITIALIZING ON PORT {port}...")
    print("="*55 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=port)