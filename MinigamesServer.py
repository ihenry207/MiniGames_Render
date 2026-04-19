from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import json
import socket  
import random
import csv
import os
import asyncio
from datetime import datetime

app = FastAPI()

# --- GAME CONSTANTS ---
LED_MEMORY_BATCH_SIZE = 10  # Number of levels per batch in LED Memory Game

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
    "guesses": {},
    
    # NEW: State tracking for timeouts
    "timer_task": None,
    "last_target": 0,
    "last_guesses": {}
}

memory_state = {
    "status": "lobby",
    "registered_players": [],
    "host_index": 0,
    "current_batch": 1,
    "base_pattern": [],
    "player_scores": {},
    "timer_task": None,
    "last_scores": {}
}

async def process_wavelength_round_end():
    """Forces the round to end, broadcasts results, and cycles the host."""
    print("\n[SERVER] Round Complete (All Guesses In or Timeout Reached).")
    
    # Save a backup of the results just in case a slow player guesses late
    wavelength_state["last_target"] = wavelength_state["target_score"]
    wavelength_state["last_guesses"] = wavelength_state["guesses"].copy()
    
    # Broadcast Results to everyone currently connected
    payload = json.dumps({
        "type": "ROUND_RESULTS",
        "target": wavelength_state["target_score"],
        "guesses": wavelength_state["guesses"]
    })
    
    for dev_id, ws in manager.active_connections.items():
        if dev_id in wavelength_state["registered_players"]:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
                
    # LOGGING: Final Round Results
    log_game_event("wavelength", "System", "ROUND_END", level=wavelength_state["current_word"], status="COMPLETED", details=f"Target: {wavelength_state['target_score']}%, Guesses: {wavelength_state['guesses']}")
    
    # ROUND ROBIN ROTATION
    total_players = len(wavelength_state["registered_players"])
    if total_players > 0:
        wavelength_state["host_index"] = (wavelength_state["host_index"] + 1) % total_players
        
    wavelength_state["status"] = "lobby"
    wavelength_state["current_word"] = ""
    wavelength_state["target_score"] = 0
    wavelength_state["guesses"] = {}
    wavelength_state["timer_task"] = None
    
    if total_players > 0:
        print(f"[SERVER] Round reset. Next host is: {wavelength_state['registered_players'][wavelength_state['host_index']]}\n")

async def wavelength_timeout_coroutine():
    """Background timer that counts to 30 and triggers the end of the round."""
    await asyncio.sleep(30)
    if wavelength_state["status"] == "guessing":
        print("\n[SERVER] 30 seconds elapsed! Forcing round to end.")
        await process_wavelength_round_end()

async def process_memory_round_end():
    """Compares all player scores and determines the winner."""
    print("\n[SERVER] Memory Round Complete (All Scores In or Timeout Reached).")
    
    # Save a backup of the results
    memory_state["last_scores"] = memory_state["player_scores"].copy()
    
    # Determine the winner
    if memory_state["player_scores"]:
        winner_id = max(memory_state["player_scores"], key=memory_state["player_scores"].get)
        winning_score = memory_state["player_scores"][winner_id]
        
        print(f"[SERVER] Round Winner: {winner_id} with score {winning_score}")
    else:
        winner_id = None
        winning_score = 0
        print(f"[SERVER] No scores submitted!")
    
    # Broadcast Results to everyone currently connected
    payload = json.dumps({
        "type": "MEMORY_ROUND_RESULTS",
        "scores": memory_state["player_scores"],
        "winner": winner_id,
        "winning_score": winning_score,
        "batch": memory_state["current_batch"]
    })
    
    for dev_id, ws in manager.active_connections.items():
        if dev_id in memory_state["registered_players"]:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
    
    # LOGGING: Final Round Results
    log_game_event("led_memory", "System", "ROUND_END", level=str(memory_state["current_batch"]), status="COMPLETED", details=f"Winner: {winner_id}, Scores: {memory_state['player_scores']}")
    
    # Reset for next round
    memory_state["status"] = "lobby"
    memory_state["current_batch"] += 1
    memory_state["player_scores"] = {}
    memory_state["timer_task"] = None

async def memory_timeout_coroutine():
    """Background timer that counts to 60 seconds for players to complete and submit the memory game."""
    await asyncio.sleep(60)
    if memory_state["status"] == "playing":
        print("\n[SERVER] 60 seconds elapsed! Forcing round to end.")
        await process_memory_round_end()

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
                    # Register player to memory game lobby if not already registered
                    if device_id not in memory_state["registered_players"]:
                        memory_state["registered_players"].append(device_id)
                        print(f"[SERVER] {device_id} joined Memory Game. Total players: {len(memory_state['registered_players'])}")
                        
                        log_game_event("led_memory", device_id, "JOIN_LOBBY", status="CONNECTED")
                    
                    # Generate base pattern for the batch
                    if memory_state["status"] == "lobby":
                        memory_state["base_pattern"] = [random.choice(["red", "green", "yellow"]) for _ in range(LED_MEMORY_BATCH_SIZE)]
                        memory_state["status"] = "playing"
                        memory_state["player_scores"] = {}
                        print(f"\n[SERVER] Memory Game Batch {memory_state['current_batch']} Started!")
                        
                        # Start 60-second timeout
                        if memory_state["timer_task"]:
                            memory_state["timer_task"].cancel()
                        memory_state["timer_task"] = asyncio.create_task(memory_timeout_coroutine())
                    
                    # Send the patterns to the player
                    patterns = [memory_state["base_pattern"][:i+1] for i in range(LED_MEMORY_BATCH_SIZE)]
                    
                    log_game_event("led_memory", device_id, "GAME_START", level=f"1-{LED_MEMORY_BATCH_SIZE}", status="PENDING")
                    
                    await websocket.send_text(json.dumps({
                        "type": "PATTERN", 
                        "patterns": patterns, 
                        "start_level": 1,
                        "batch": memory_state["current_batch"],
                        "multiplayer": True,
                        "num_players": len(memory_state["registered_players"])
                    }))
                
                # --- WAVELENGTH GAME LOGIC ---
                elif game_selected == "wavelength":
                    if device_id not in wavelength_state["registered_players"]:
                        wavelength_state["registered_players"].append(device_id)
                        print(f"[SERVER] {device_id} joined Wavelength. Total players: {len(wavelength_state['registered_players'])}")
                        
                        log_game_event("wavelength", device_id, "JOIN_LOBBY", status="CONNECTED")

                    current_host_id = wavelength_state["registered_players"][wavelength_state["host_index"]]

                    if device_id == current_host_id:
                        wavelength_state["status"] = "host_choosing"
                        
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
                print("[SERVER] 30-Second Guessing Timer Started!")
                
                log_game_event("wavelength", device_id, "HOST_LOCKED_IN", level=selected_word, status="PENDING_GUESSES", details=f"Target: {target_score}%")
                
                # Cancel existing timer if one is somehow running, then start a new 30-second countdown
                if wavelength_state["timer_task"]:
                    wavelength_state["timer_task"].cancel()
                wavelength_state["timer_task"] = asyncio.create_task(wavelength_timeout_coroutine())

            # ---------------------------------------------------------
            # 3. WAVELENGTH PLAYER UPLOADS GUESS
            # ---------------------------------------------------------
            elif msg_type == "PLAYER_GUESS":
                # ANTI-STUCK: If the round already ended due to timeout, unstick the late player
                if wavelength_state["status"] != "guessing":
                    print(f"[SERVER] Late guess from {device_id} ignored. Sending old results to unstick board.")
                    await websocket.send_text(json.dumps({
                        "type": "ROUND_RESULTS",
                        "target": wavelength_state["last_target"],
                        "guesses": wavelength_state["last_guesses"]
                    }))
                    continue # Skip the rest of the scoring logic
                    
                guess_score = message.get("score")
                wavelength_state["guesses"][device_id] = guess_score
                print(f"[SERVER] Received guess from {device_id}: {guess_score}%")
                
                log_game_event("wavelength", device_id, "PLAYER_GUESSED", level=wavelength_state["current_word"], status="GUESSED", details=f"Guess: {guess_score}%")
                
                total_players = len(wavelength_state["registered_players"])
                
                # Check if all players (except the host) have guessed
                if len(wavelength_state["guesses"]) >= (total_players - 1) and total_players > 1:
                    print("\n[SERVER] ALL GUESSES IN! Ending round early.")
                    
                    # Cancel the 30-second timeout since everyone was fast enough
                    if wavelength_state["timer_task"]:
                        wavelength_state["timer_task"].cancel()
                        
                    await process_wavelength_round_end()

            # ---------------------------------------------------------
            # 4. MEMORY GAME RESULTS
            # ---------------------------------------------------------
            elif msg_type == "GAME_RESULTS":
                score = message.get("score", 0)
                device = message.get("device_id", device_id)
                
                print(f"\n[SERVER] [RESULTS] Received score from {device}: {score}")
                
                # Handle multiplayer memory game
                if memory_state["status"] == "playing":
                    memory_state["player_scores"][device] = score
                    
                    log_game_event("led_memory", device, "SCORE_SUBMITTED", level=str(memory_state["current_batch"]), status="SUBMITTED", details=f"Score: {score}")
                    
                    total_players = len(memory_state["registered_players"])
                    
                    # Check if all players have submitted their scores
                    if len(memory_state["player_scores"]) >= total_players and total_players > 1:
                        print("\n[SERVER] ALL SCORES IN! Ending round early.")
                        
                        # Cancel the 60-second timeout since everyone was fast enough
                        if memory_state["timer_task"]:
                            memory_state["timer_task"].cancel()
                            
                        await process_memory_round_end()
                    
                    elif total_players == 1:
                        # Single player mode - just log and acknowledge
                        log_game_event("led_memory", device, "SINGLE_PLAYER_GAME", level=str(score), status="COMPLETED", details=f"Score: {score}")
                        print(f"[SERVER] Single player game completed with score {score}")

    except WebSocketDisconnect:
        manager.disconnect(device_id)

if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.environ.get("PORT", 8000)) 

    print("\n" + "="*55)
    print(f"  MINIGAMES HOST SERVER INITIALIZING ON PORT {port}...")
    print("="*55 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=port)