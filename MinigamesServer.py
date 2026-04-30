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
#test push - delete me
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

# =========================================================
# RPS GAME STATE AND HELPERS
# =========================================================

rps_waiting_queue = []
rps_games = {}
rps_player_game = {}

async def safe_send(device_id: str, payload: dict):
    """Send a JSON message to a connected device."""
    ws = manager.active_connections.get(device_id)

    if not ws:
        return False

    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception as e:
        print(f"[SERVER] Failed to send to {device_id}: {e}")
        return False

def build_rps_game_id():
    return f"rps_{random.randint(1000, 9999)}"

def cleanup_rps_game(game_id: str):
    """Remove finished RPS game state."""
    game = rps_games.pop(game_id, None)

    if not game:
        return

    for player in game["players"]:
        if rps_player_game.get(player) == game_id:
            del rps_player_game[player]

def resolve_rps(selection_a: str, selection_b: str):
    """
    Return result for both players.
    Returns: (result_for_a, result_for_b)
    """
    if selection_a == "forfeit" and selection_b == "forfeit":
        return "tie", "tie"

    if selection_a == "forfeit":
        return "lose", "win"

    if selection_b == "forfeit":
        return "win", "lose"

    if selection_a == selection_b:
        return "tie", "tie"

    wins_against = {
        "rock": "scissors",
        "paper": "rock",
        "scissors": "paper"
    }

    if wins_against.get(selection_a) == selection_b:
        return "win", "lose"

    return "lose", "win"

async def try_start_rps_match():
    """Pair the first two waiting RPS players."""
    while len(rps_waiting_queue) >= 2:
        p1 = rps_waiting_queue.pop(0)
        p2 = rps_waiting_queue.pop(0)

        # Skip disconnected players.
        if p1 not in manager.active_connections or p2 not in manager.active_connections:
            continue

        game_id = build_rps_game_id()

        rps_games[game_id] = {
            "players": [p1, p2],
            "selections": {}
        }

        rps_player_game[p1] = game_id
        rps_player_game[p2] = game_id

        await safe_send(p1, {
            "type": "RPS_READY",
            "game_id": game_id,
            "message": "Opponent found! Start round.",
            "opponent": p2
        })

        await safe_send(p2, {
            "type": "RPS_READY",
            "game_id": game_id,
            "message": "Opponent found! Start round.",
            "opponent": p1
        })

        log_game_event(
            "rps",
            "System",
            "MATCH_START",
            level=game_id,
            status="READY",
            details=f"{p1} vs {p2}"
        )

        print(f"[SERVER] RPS match started: {p1} vs {p2}")

async def handle_rps_disconnect(device_id: str):
    """Clean up RPS state when a device disconnects."""
    if device_id in rps_waiting_queue:
        rps_waiting_queue.remove(device_id)

    game_id = rps_player_game.get(device_id)

    if not game_id:
        return

    game = rps_games.get(game_id)

    if not game:
        rps_player_game.pop(device_id, None)
        return

    players = game["players"]
    opponent = players[1] if players[0] == device_id else players[0]

    await safe_send(opponent, {
        "type": "RPS_RESULT",
        "result": "win",
        "game_id": game_id,
        "opponent_selection": "disconnect"
    })

    log_game_event(
        "rps",
        device_id,
        "DISCONNECT",
        level=game_id,
        status="LOSS",
        details=f"Opponent {opponent} awarded win"
    )

    cleanup_rps_game(game_id)

# --- GAME STATES ---
game_states = {} 

wavelength_state = {
    "status": "lobby", 
    "registered_players": [], 
    "host_index": 0, 
    "category_options": [
        "1: Hot - Cold",
        "2: Useless - Useful",
        "3: Soft - Hard",
        "4: Trashy - Classy",
        "5: Boring - Exciting"
    ],
    "cat1_options": [
        "1: Coffee",
        "2: California",
        "3: Indiana"
    ],
    "cat2_options": [
        "1: Paperclip",
        "2: Smartphone",
        "3: Spoon"
    ],
    "cat3_options": [   
        "1: Pillow",
        "2: Brick",
        "3: Cotton Candy"
    ],
    "cat4_options": [
        "1: Reality TV",
        "2: Opera",
        "3: Action Movies"
    ],
    "cat5_options": [
        "1: Watching Paint Dry",
        "2: Roller Coasters",
        "3: Skydiving"
    ],
    "current_category": "",
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

                    # Extend pattern instead of replacing it
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

                # --- RPS GAME LOGIC ---
                elif game_selected == "rps":
                    # Player is already in an active game.
                    if device_id in rps_player_game:
                        game_id = rps_player_game[device_id]
                        game = rps_games.get(game_id)

                        if game:
                            opponent = (
                                game["players"][1]
                                if game["players"][0] == device_id
                                else game["players"][0]
                            )

                            await websocket.send_text(json.dumps({
                                "type": "RPS_READY",
                                "game_id": game_id,
                                "message": "Rejoined active match.",
                                "opponent": opponent
                            }))

                        else:
                            rps_player_game.pop(device_id, None)

                            await websocket.send_text(json.dumps({
                                "type": "RPS_WAITING",
                                "message": "Waiting for opponent..."
                            }))

                    # Player is already waiting.
                    elif device_id in rps_waiting_queue:
                        await websocket.send_text(json.dumps({
                            "type": "RPS_WAITING",
                            "message": "Waiting for opponent..."
                        }))

                    # New player joins queue.
                    else:
                        rps_waiting_queue.append(device_id)

                        log_game_event(
                            "rps",
                            device_id,
                            "JOIN_QUEUE",
                            status="WAITING"
                        )

                        await websocket.send_text(json.dumps({
                            "type": "RPS_WAITING",
                            "message": "Waiting for opponent..."
                        }))

                        await try_start_rps_match()
                
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
                            "categories": wavelength_state["category_options"],
                            "cat1_words": wavelength_state["cat1_options"],
                            "cat2_words": wavelength_state["cat2_options"],
                            "cat3_words": wavelength_state["cat3_options"],
                            "cat4_words": wavelength_state["cat4_options"],
                            "cat5_words": wavelength_state["cat5_options"]
                        }))
                        logger.info(f"Sent Host options to {device_id}.")
                    
                    else:
                        if wavelength_state["status"] == "guessing":
                            await websocket.send_text(json.dumps({
                                "type": "WAVELENGTH_ROLE",
                                "role": "player_guess",
                                "category": wavelength_state["current_category"],
                                "word": wavelength_state["current_word"]
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "WAVELENGTH_ROLE",
                                "role": "player_wait"
                            }))
            # ---------------------------------------------------------
            # 2. RPS COLLECTS SELECTION FROM PLAYERS
            # ---------------------------------------------------------                
            elif msg_type == "RPS_SELECTION":
                game_id = rps_player_game.get(device_id)

                if not game_id or game_id not in rps_games:
                    await websocket.send_text(json.dumps({
                        "type": "RPS_WAITING",
                        "message": "Waiting for opponent..."
                    }))
                    continue

                game = rps_games[game_id]
                players = game["players"]

                opponent = (
                    players[1]
                    if players[0] == device_id
                    else players[0]
                )

                selection = message.get("selection", "forfeit")
                game["selections"][device_id] = selection

                log_game_event(
                    "rps",
                    device_id,
                    "SELECTION",
                    level=game_id,
                    status="LOCKED_IN",
                    details=selection
                )

                print(f"[SERVER] RPS selection from {device_id}: {selection}")

                # Wait until both players submit.
                if opponent not in game["selections"]:
                    await websocket.send_text(json.dumps({
                        "type": "RPS_WAITING",
                        "message": "Selection received. Waiting for opponent..."
                    }))
                    continue

                p1, p2 = players
                sel1 = game["selections"][p1]
                sel2 = game["selections"][p2]

                res1, res2 = resolve_rps(sel1, sel2)

                await safe_send(p1, {
                    "type": "RPS_RESULT",
                    "result": res1,
                    "game_id": game_id,
                    "opponent_selection": sel2
                })

                await safe_send(p2, {
                    "type": "RPS_RESULT",
                    "result": res2,
                    "game_id": game_id,
                    "opponent_selection": sel1
                })

                log_game_event(
                    "rps",
                    "System",
                    "ROUND_END",
                    level=game_id,
                    status=f"{p1}:{res1},{p2}:{res2}",
                    details=f"{p1}={sel1}, {p2}={sel2}"
                )

                print(
                    f"[SERVER] RPS result: "
                    f"{p1}={sel1}/{res1}, {p2}={sel2}/{res2}"
                )

                cleanup_rps_game(game_id)

            # ---------------------------------------------------------
            # 2. WAVELENGTH HOST UPLOADS TARGET
            # ---------------------------------------------------------
            elif msg_type == "HOST_SUBMIT":
                selected_word = message.get("word") 
                category_index = message.get("category_index")
                target_score = message.get("score")
                
                wavelength_state["current_word"] = selected_word
                wavelength_state["current_category"] = wavelength_state["category_options"][category_index]
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

                    required_length = next_start + LED_MEMORY_BATCH_SIZE - 1

                    # Extend global pattern if needed
                    while len(memory_state["pattern"]) < required_length:
                        memory_state["pattern"].append(
                            random.choice(["red", "green", "yellow"])
                        )

                    patterns = [
                        memory_state["pattern"][:i]
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
