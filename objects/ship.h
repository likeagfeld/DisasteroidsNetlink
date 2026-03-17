#pragma once

void updateTitleScreenShips(void);
void drawTitleScreenShips(void);
void destroyPlayer(PPLAYER player);

void drawPlayers(void);

void spawnPlayer(PPLAYER player, int angle, int respawnTimer);
void spawnPlayers(void);

void initPlayers(void);
unsigned int countAlivePlayers(void);

/* Server-authoritative functions (online mode) */
void destroyPlayerFromServer(int player_id, int lives, int angle,
                             int invuln, int respawn);
void spawnPlayerFromServer(int player_id, int angle, int invuln);
