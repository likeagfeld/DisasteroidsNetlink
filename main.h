#pragma once

#include <jo/jo.h>
#include <string.h>
#include "util.h"
#include "state.h"
#include "gameplay_constants.h"
#include "objects/player.h"

#define GAME_VERSION "1.10"
#define MAX_DEBUG_LEVEL (1)
#ifndef DNET_MAX_NAME
#define DNET_MAX_NAME 16
#endif

// supported game types
typedef enum _GAME_TYPE
{
    GAME_TYPE_COOP = 0,
    GAME_TYPE_VERSUS,
    GAME_TYPE_MAX,
} GAME_TYPE;

// number of lives per game
typedef enum _GAME_LIVES
{
    GAME_LIVES_1,
    GAME_LIVES_3,
    GAME_LIVES_6,
    GAME_LIVES_9,
    GAME_LIVES_MAX,
} GAME_LIVES;

// represents game state
typedef struct _GAME
{
    // current game state
    GAME_STATE gameState;

    // coop or versus
    GAME_TYPE gameType;

    jo_camera camera;

    // debug level
    int debug;

    // number of lives (1, 3, 6, 9)
    int numLives;

    // changeable HUD color
    jo_color hudColor;

    // is the game is paused
    bool isPaused;

    // is the game finished?
    bool isGameOver;

    // how many frames since all players were dead
    int gameOverFrames;

    // how long to wait before moving diasteroids
    int disasteroidSpawnFrames;

    // current game wave
    // higher = more disasteroids, faster speed
    int wave;

    // hack to cache controller inputs
    // used for menus by player 1
    INPUTCACHE input;

    // online multiplayer
    bool isOnlineMode;

    // which player index this Saturn controls (assigned by server)
    unsigned char myPlayerID;

    // second local player ID (0xFF = none)
    unsigned char myPlayerID2;

    // true if second controller detected for online
    bool hasSecondLocal;

    // player names for online
    char playerName[DNET_MAX_NAME + 1];
    char playerName2[DNET_MAX_NAME + 1];

    // frame counter for network input sync
    unsigned int netFrameCount;

} GAME, *PGAME;

// globals
extern PLAYER g_Players[MAX_PLAYERS];
extern GAME g_Game;

// Second controller port detection.
// Without a multitap, Port B (second physical controller port) maps to
// jo_inputs[6] in Jo Engine's input array. With a multitap on Port A,
// the second device maps to jo_inputs[1]. Check both.
static inline int getP2Port(void)
{
    if (jo_is_input_available(1)) return 1;   /* multitap slot */
    if (jo_is_input_available(6)) return 6;   /* Port B direct */
    return -1;
}
