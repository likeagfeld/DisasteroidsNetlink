================================================================================
                        DISASTEROIDS - ONLINE GUIDE
================================================================================

PLAYING THE GAME
----------------
Load game.cue (NOT game.iso) in your emulator or disc burner to get CD audio
music. The following 6 files must all stay in the same folder:

  game.cue
  game.iso
  02_TITLE.WAV
  03_PAUSE.WAV
  04_VICTORY.WAV
  05_GAMEOVER.WAV


ONLINE PLAY OVERVIEW
--------------------
Disasteroids supports up to 12 players online using the Sega Saturn NetLink
modem. The game connects through a phone line to a bridge application on your
PC, which relays traffic to the game server over the internet.

Two bridge options are provided:
  - DreamPi   : For Raspberry Pi-based DreamPi setups (Linux)
  - PC Bridge : For Windows/Mac/Linux PCs with a USB modem


DREAMPI SETUP
-------------
1. Copy the files from Online\DreamPi\ to your DreamPi:
     netlink.py  -> /opt/dreampi/
     config.ini  -> /opt/dreampi/

2. Restart the DreamPi service:
     sudo systemctl restart dreampi

3. To use a custom server, edit config.ini:
     [server.778]
     host = your.server.address
     port = 4822


PC / WINDOWS SETUP
------------------
1. Connect a USB modem to your PC and your Saturn NetLink to a phone line.
   Connect the PC modem to the same phone line (or use a phone-line simulator).

2. Double-click start_bridge.bat in the Online\PC Bridge\ folder.

3. Select your COM port when prompted.

The bridge will connect to the default game server automatically.


MAC / LINUX SETUP
-----------------
Run bridge.py directly from a terminal:

  python bridge.py --port /dev/ttyUSB0 --server disasteroids.server.address --server-port 4822

Replace /dev/ttyUSB0 with your modem's serial port.


LOBBY CONTROLS
--------------
  A Button    Start / Ready up
  B Button    Back to title screen (stay connected)
  Y Button    Disconnect and return to title screen
  Z Button    Hold to view leaderboard / last game results


SERVER HOSTING
--------------
The game server is located in tools\disasteroids_server\dserver.py.

Basic usage:
  python dserver.py

With bots:
  python dserver.py --bots 3

The server listens on port 4822 by default.

For Linux deployment, a systemd service file is provided:
  tools\disasteroids_server\disasteroids.service

Install it with:
  sudo cp disasteroids.service /etc/systemd/system/
  sudo systemctl enable disasteroids
  sudo systemctl start disasteroids

================================================================================
