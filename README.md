# Disclaimer - Online Netlink functionality has been implemented with assistance of AI (Claude).

## Online Multiplayer (NetLink)
Disasteroids now supports online multiplayer via the Sega Saturn NetLink modem. Up to 12 players can connect to a central server and play cooperatively or competitively over the internet.

### Features
- **Server-authoritative gameplay**: Asteroid spawning, wave progression, scoring, and ship-asteroid collisions are all managed by the server to prevent cheating and ensure consistency
- **Custom name entry**: Grid-based character entry screen for choosing your online handle (saved to Saturn backup RAM across power cycles)
- **Online lobby**: See connected players, toggle ready status, start the game when all players are ready
- **Winner display**: After a versus game, the winner's name shows "WIN!" in the lobby player list
- **Persistent leaderboard**: Server tracks wins, best score, and games played per player across sessions
- **Z-button stats overlay**: Hold Z in the lobby to view last game results and the all-time online leaderboard (alternates every 3 seconds)
- **Server bots**: The host can add/remove AI-controlled bot players (easy/medium/hard difficulty) from the lobby
- **Couch co-op online**: Connect a second controller locally and both players join the online game from the same Saturn
- **Score screen with names**: The end-of-game ranking screen displays player names in online mode
- **Delta-compressed input**: Inputs are only transmitted when they change, minimizing bandwidth on the 14,400 baud modem link
- **Ship state interpolation**: Remote player ships are smoothly interpolated to reduce visual jitter
- **Name persistence**: Your name is saved to Saturn backup RAM and automatically loaded on the next power-on

### How to Connect
1. Select **ONLINE** from the title screen
2. Enter your name on the character grid (D-pad to move, A/C to select, B to cancel)
3. The Saturn dials out via the NetLink modem to the bridge server
4. Once in the lobby, press **Start** to toggle ready; press **A** to start the game when all players are ready
5. Use **L/R** to add/remove bots, **X** to cycle bot difficulty

### Lobby Controls
- **Start**: Toggle ready status
- **A**: Start game (when all players are ready)
- **L/R**: Add/remove bots
- **X**: Cycle bot difficulty
- **Z** (hold): View last game results and all-time leaderboard
- **B**: Return to title screen (stays connected)
- **Y**: Disconnect and return to title screen

### Online Setup — DreamPi

If you have a [DreamPi](https://github.com/Kazade/dreampi) (Raspberry Pi with USB modem for retro online gaming), you can replace its default netlink handler with the one in this repo to route Disasteroids dial codes to the game server.

1. Copy `tools/dreampi/netlink.py` to your DreamPi, replacing the existing file:
   ```bash
   sudo cp tools/dreampi/netlink.py /opt/dreampi/netlink.py
   ```
2. Copy `tools/dreampi/config.ini` to your DreamPi:
   ```bash
   sudo cp tools/dreampi/config.ini /opt/dreampi/config.ini
   ```
3. Edit `/opt/dreampi/config.ini` if you need to change the server host/port (defaults point to `saturncoup.duckdns.org:4822`)
4. Restart DreamPi:
   ```bash
   sudo systemctl restart dreampi
   ```

The `config.ini` maps dial codes to game servers. When the Saturn dials `#778#`, the DreamPi routes the connection to the Disasteroids server. The `netlink.py` handles modem negotiation, authentication via shared secret, and creates a transparent TCP tunnel.

### Online Setup — PC (No DreamPi)

If you don't have a DreamPi, you can connect a USB modem directly to a PC and use `bridge.py`:

1. Connect a Hayes-compatible USB modem to your PC
2. Connect the modem to the Saturn NetLink via phone cable

**Windows (easy):**

3. Double-click `tools/netlink_bridge/start_bridge.bat`
4. It will auto-install pyserial if needed, list your COM ports, and prompt you to pick your modem
5. That's it — the bridge connects to the Disasteroids server automatically

**Linux / macOS / manual:**

3. Run the bridge:
   ```bash
   cd tools/netlink_bridge
   python bridge.py --serial-port /dev/ttyUSB0 --server saturncoup.duckdns.org:4822 --secret "SaturnDisasteroids2026!NetLink#Key"
   ```

The bridge listens for the Saturn's dial-out, answers the call, and tunnels data to the game server over TCP.

### Server Setup

The Python game server is in `tools/disasteroids_server/`:

```bash
cd tools/disasteroids_server
python dserver.py --port 4822 --bots 2
```

Options:
- `--port PORT` — TCP listen port (default: 4822)
- `--bots N` — Number of AI bot players to add (default: 0)
- `--secret KEY` — Shared secret for client authentication (must match config.ini/bridge)

The server stores a persistent leaderboard in `leaderboard.json` next to `dserver.py`. This file tracks wins, best score, and games played per player name across sessions.

For production deployment on Linux, a systemd service file is provided:
```bash
sudo cp tools/disasteroids_server/disasteroids.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable disasteroids
sudo systemctl start disasteroids
```

# Disasteroids
Disasteroids is a 12-player Asteroids clone for the Sega Saturn. Requires two [6 Player Adaptors](https://segaretro.org/Saturn_6_Player_Adaptor) for full twelve player support. Requires a modded Saturn or another method to get code running on actual hardware. Build the code with Jo Engine or grab an ISO from [releases](https://github.com/likeagfeld/DisasteroidsNetlink/releases).  

Disasteroids was my entry to the [Sega Saturn 28th Anniversary Game Competition](https://segaxtreme.net/threads/sega-saturn-28th-anniversary-game-competition.25278/).  

## Screenshots
![Sega Saturn Multiplayer Task Force](screenshots/ssmtf.png)
![Twelve Snakes Title](screenshots/title.png)
![Multiplayer](screenshots/gameplay.png)
![Solo](screenshots/solo.png)
![Score](screenshots/score.png)

## How to Play
* Plug in as many controllers/multitaps as you have  
* Select a game mode (CO-OP or Versus)  
  * CO-OP mode is 1-12 players. Try to last for as many waves as possible. Game ends when all players are out of lives. Colliding players will bounce off of each other. Player projectiles will cause other players to bounce, not die.  
  * Versus mode will not start if you do not have at least two controllers plugged in. Game ends when there is only one player left. Player projectiles will destroy other players  
* Try is the number of lives  
* HUD is the color of the display  

## Game Controls
* A/C to shoot
* Up/B to thrust
* Left/Right to angle your ship
* X to change your ship color (doesn't work if all 12 colors are used)

## Player One Special Commands
Only player one can:  
* interact with the menus  
* pause/display the score with the Start button  
* Press Y to change the HUD color  
* Press Z for debugging info  
* Press ABC + Start to reset the game   

## Burning
On Linux I was able to burn the ISO/CUE + WAV with: cdrdao write --force game.cue.

## Building
Requires Jo Engine to build. Checkout source code folder to your Jo Engine "Projects" directory and run `compile.bat` (Windows) or `./compile.sh` (Linux). The build generates `game.iso` and a `game.cue` sheet that references the audio tracks.

**Important**: When running in an emulator, always load `game.cue` (not `game.iso` directly) to get CD audio music playback. The ISO alone contains only the data track.
 
## Credits
Thank you to Reyeme for the disasteroid generation algorithm, advice   
Thank you to EmeraldNova and KnightOfDragon for basic geometry refresher, advice   
Lots of advice and feedback from the #segaxtreme Discord (Fafling, Ndiddy, Ponut, and more)   
Tutorial I got ideas from: [Code Asteroids in JavaScript (1979 Atari game) - tutorial](https://www.youtube.com/watch?v=H9CSWMxJx84
)   
Title Song: [Powerup!](https://www.youtube.com/watch?v=l7SwiFWOQqM) by Jeremy Blake. No Copyright.  
Game Over Song: [Dub Hub](https://www.youtube.com/watch?v=in8hEbX9mM8) by Jimmy Fontanez. No Copyright.  
Thank you to [Emerald Nova](www.emeraldnova.com) for organizing the Saturn Dev contest  
[SegaXtreme](http://www.segaxtreme.net/) - The best Sega Saturn development forum on the web. Thank you for all the advice from all the great posters on the forum.  
[Jo Engine](https://github.com/johannes-fetz/joengine) - Sega Saturn dev environment

