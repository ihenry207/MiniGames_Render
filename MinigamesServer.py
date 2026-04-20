from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import json
import socket  
import random
import csv
import os
import asyncio
from datetime import datetime
import logging
import sys

# --- LOGGING SETUP FOR RENDER ---
# This forces logs to stream directly to Render's console instantly
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("minigames_server")

app = FastAPI()


# --- GAME CONSTANTS ---
LED_MEMORY_BATCH_SIZE = 10  # Number of levels per batch in LED Memory Game

memory_state = {
    "status": "lobby",
    "registered_players": [],
    "pattern": [],
    "scores": {},
    "levels": {},
    "active_players": set(),
}
# --- CSV LOGGING SETUP ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

def log_game_event(game_name: str, device_id: str, event_type: str, level: str = "N/A", status: str = "N/A", details: str = ""):
    filename = os.path.join(DATA_DIR, f"{game_name}.csv")
    file_exists = os.path.isfile(filename)
    try:
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
    except Exception as e:
        logger.error(f"Failed to write to CSV {filename}: {e}")

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[device_id] = websocket
        logger.info(f"[+] Node Connected: {device_id}")

    def disconnect(self, device_id: str):
        if device_id in self.active_connections:
            del self.active_connections[device_id]
            logger.info(f"[-] Node Disconnected: {device_id}")

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

async def process_wavelength_round_end():
    """Forces the round to end, broadcasts results, and cycles the host."""
    logger.info("Round Complete (All Guesses In or Timeout Reached).")
    
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
            except Exception as e:
                logger.warning(f"Failed to send to {dev_id}: {e}")
                
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
        next_host = wavelength_state['registered_players'][wavelength_state['host_index']]
        logger.info(f"Round reset. Next host is: {next_host}")

async def wavelength_timeout_coroutine():
    """Background timer that counts to 30 and triggers the end of the round."""
    await asyncio.sleep(30)
    if wavelength_state["status"] == "guessing":
        logger.info("30 seconds elapsed! Forcing round to end.")
        await process_wavelength_round_end()

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
                logger.info(f"{device_id} selected: {game_selected}")
                

                # --- MEMORY GAME LOGIC (MULTIPLAYER) ---
                if game_selected == "led_memory":
                    if device_id not in memory_state["registered_players"]:
                        memory_state["registered_players"].append(device_id)
                        logger.info(f"{device_id} joined LED Memory. Total players: {len(memory_state['registered_players'])}")
                        log_game_event("led_memory", device_id, "JOIN_LOBBY", status="CONNECTED")

                    # If first player, generate pattern and reset state
                    if memory_state["status"] == "lobby":
                        memory_state["levels"] = {}
                        memory_state["active_players"] = set(memory_state["registered_players"])
                        memory_state["status"] = "playing"
                        memory_state["pattern"] = [random.choice(["red", "green", "yellow"]) for _ in range(LED_MEMORY_BATCH_SIZE)]
                        memory_state["scores"] = {}
                        memory_state["levels"][device_id] = 1
   

                    start_level = memory_state["levels"][device_id]

                    required_length = start_level + LED_MEMORY_BATCH_SIZE - 1

                    # Extend pattern if this player has advanced further than the current pattern length
                    while len(memory_state["pattern"]) < required_length:

                        memory_state["pattern"].append(
                            random.choice(["red", "green", "yellow"])
                        )

                    patterns = [
                        memory_state["pattern"][:i]
                        for i in range(start_level, start_level + LED_MEMORY_BATCH_SIZE)
                    ]

                    await websocket.send_text(json.dumps({
                        "type": "PATTERN",
                        "patterns": patterns,
                        "start_level": start_level
                    }))
                
                # --- WAVELENGTH GAME LOGIC ---
                elif game_selected == "wavelength":
                    if device_id not in wavelength_state["registered_players"]:
                        wavelength_state["registered_players"].append(device_id)
                        logger.info(f"{device_id} joined Wavelength. Total players: {len(wavelength_state['registered_players'])}")
                        
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
                        logger.info(f"Sent Host options to {device_id}.")
                    
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
                
                logger.info(f"HOST LOCKED IN! Word: '{selected_word}' | Target: {target_score}%")
                logger.info("30-Second Guessing Timer Started!")
                
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
                    logger.info(f"Late guess from {device_id} ignored. Sending old results to unstick board.")
                    await websocket.send_text(json.dumps({
                        "type": "ROUND_RESULTS",
                        "target": wavelength_state["last_target"],
                        "guesses": wavelength_state["last_guesses"]
                    }))
                    continue # Skip the rest of the scoring logic
                    
                guess_score = message.get("score")
                wavelength_state["guesses"][device_id] = guess_score
                logger.info(f"Received guess from {device_id}: {guess_score}%")
                
                log_game_event("wavelength", device_id, "PLAYER_GUESSED", level=wavelength_state["current_word"], status="GUESSED", details=f"Guess: {guess_score}%")
                
                total_players = len(wavelength_state["registered_players"])
                
                # Check if all players (except the host) have guessed
                if len(wavelength_state["guesses"]) >= (total_players - 1) and total_players > 1:
                    logger.info("ALL GUESSES IN! Ending round early.")
                    
                    # Cancel the 30-second timeout since everyone was fast enough
                    if wavelength_state["timer_task"]:
                        wavelength_state["timer_task"].cancel()
                        
                    await process_wavelength_round_end()


            # ---------------------------------------------------------
            # 4. MEMORY GAME RESULTS (MULTIPLAYER)
            # ---------------------------------------------------------
            elif msg_type == "GAME_RESULTS":

                device = message.get("device_id")
                score = int(message.get("score", 0))

                logger.info(f"[RESULTS] {device}: {score}")

                start_level = memory_state["levels"][device]
                expected_score = start_level + LED_MEMORY_BATCH_SIZE - 1

                # ----------------------------------
                # CASE 1 — Player FAILED
                # ----------------------------------
                if score < expected_score:

                    memory_state["scores"][device] = score
                    memory_state["active_players"].discard(device)

                    logger.info(f"{device} finished with score {score}")

                # ----------------------------------
                # CASE 2 — Player PERFECT BATCH
                # ----------------------------------
                else:

                    memory_state["levels"][device] = score + 1

                    logger.info(f"{device} requesting next batch")

                    next_start = memory_state["levels"][device]

                    pattern = [
                        random.choice(["red", "green", "yellow"])
                        for _ in range(next_start + LED_MEMORY_BATCH_SIZE - 1)
                    ]

                    patterns = [
                        pattern[:i]
                        for i in range(
                            next_start,
                            next_start + LED_MEMORY_BATCH_SIZE
                        )
                    ]

                    ws = manager.active_connections.get(device)

                    if ws:
                        await ws.send_text(json.dumps({
                            "type": "PATTERN",
                            "patterns": patterns,
                            "start_level": next_start
                        }))

                    return

                # ----------------------------------
                # END GAME WHEN ALL PLAYERS DONE
                # ----------------------------------
                if len(memory_state["active_players"]) == 0:

                    max_score = max(memory_state["scores"].values())

                    winners = [
                        dev
                        for dev, sc in memory_state["scores"].items()
                        if sc == max_score
                    ]

                    result_payload = json.dumps({
                        "type": "MEMORY_RESULTS",
                        "scores": memory_state["scores"],
                        "winners": winners
                    })

                    for dev_id in memory_state["registered_players"]:
                        ws = manager.active_connections.get(dev_id)
                        if ws:
                            await ws.send_text(result_payload)

                    logger.info("All players finished.")

                    memory_state["status"] = "lobby"
                    memory_state["registered_players"] = []
                    memory_state["scores"] = {}
                    memory_state["levels"] = {}
                    memory_state["active_players"] = set()

    except WebSocketDisconnect:
        logger.info(f"WebSocket closed by client: {device_id}")
        manager.disconnect(device_id)
    except Exception as e:
        logger.error(f"Unexpected error with client {device_id}: {e}", exc_info=True)
        manager.disconnect(device_id)

if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.environ.get("PORT", 8000)) 

    logger.info("="*55)
    logger.info(f"  MINIGAMES HOST SERVER INITIALIZING ON PORT {port}...")
    logger.info("="*55)
    
    uvicorn.run(app, host="0.0.0.0", port=port)
