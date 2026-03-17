# Disclaimer - Online Netlink functionality has been implemented with assistance of AI (Claude).

## Online Multiplayer (NetLink)
Disasteroids now supports online multiplayer via the Sega Saturn NetLink modem. Up to 12 players can connect to a central server and play cooperatively or competitively over the internet.

### Features
- **Server-authoritative gameplay**: Asteroid spawning, wave progression, scoring, and ship-asteroid collisions are all managed by the server to prevent cheating and ensure consistency
- **Custom name entry**: Grid-based character entry screen for choosing your online handle
- **Online lobby**: See connected players, toggle ready status, start the game when all players are ready
- **Server bots**: The host can add/remove AI-controlled bot players (easy/medium/hard difficulty) from the lobby
- **Couch co-op online**: Connect a second controller locally and both players join the online game from the same Saturn
- **Score screen with names**: The end-of-game ranking screen displays player names in online mode
- **Delta-compressed input**: Inputs are only transmitted when they change, minimizing bandwidth on the 14,400 baud modem link
- **Ship state interpolation**: Remote player ships are smoothly interpolated to reduce visual jitter

### How to Connect
1. Select **ONLINE** from the title screen
2. Enter your name on the character grid (D-pad to move, A to select, C to backspace, Start to confirm)
3. The Saturn dials out via the NetLink modem to the bridge server
4. Once in the lobby, press **Start** to toggle ready; press **A** to start the game when all players are ready
5. Use **L/R** to add/remove bots, **X** to cycle bot difficulty

### Server Setup
The Python game server and serial-to-TCP bridge are in the `tools/` directory:
- `tools/disasteroids_server/dserver.py` — Game server (default port 4822)
- `tools/netlink_bridge/bridge.py` — Serial-to-TCP bridge for connecting USB modems to the server

# Disasteroids
Disasteroids is a 12-player Asteroids clone for the Sega Saturn. Requires two [6 Player Adaptors](https://segaretro.org/Saturn_6_Player_Adaptor) for full twelve player support. Requires a modded Saturn or another method to get code running on actual hardware. Build the code with Jo Engine or grab an ISO from [releases](https://github.com/slinga-homebrew/Disasteroids/releases).  

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
Requires Jo Engine to build. Checkout source code folder to your Jo Engine "Projects" directory and run "./compile.sh".   
 
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

