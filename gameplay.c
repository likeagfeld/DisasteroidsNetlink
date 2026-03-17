#include <jo/jo.h>
#include "main.h"
#include "gameplay.h"
#include "assets/assets.h"
#include "collision.h"
#include "pause.h"
#include "objects/alien.h"
#include "objects/ship.h"
#include "objects/disasteroid.h"
#include "objects/explosion.h"
#include "objects/projectile.h"
#include "objects/star.h"
#include "net/disasteroids_net.h"
#include "net/disasteroids_protocol.h"

// players
static void getGameplayPlayersInput(void);
static uint16_t packLocalInput(int playerID);
static void getOnlinePlayersInput(void);
static void applyInputBitsToPlayer(PPLAYER player, uint16_t bits, bool isLocal);
static void updateGameplayPlayers(void);

// gameplay state
static void initNewGame(void);
static void initWave(void);
static void isGameOver(void);
static void isWaveOver(void);

// misc
static void drawGameplayWave(void);

//
// Gameplay Callbacks
//

// initialize new game
void gameplay_init(void)
{
    initNewGame();

    initPlayers();
    initExplosions();
    initShipDebris();
    initDisasteroids();
    initStars();
    initAlien();

    g_Game.netFrameCount = 0;

    initWave();
}

// gameplay input callback
void gameplay_input(void)
{
    if(g_Game.gameState != GAME_STATE_GAMEPLAY)
    {
        return;
    }

    if(g_Game.isPaused == true)
    {
        return;
    }

    if(g_Game.isOnlineMode)
    {
        getOnlinePlayersInput();
    }
    else
    {
        getGameplayPlayersInput();
    }
}

// gameplay update callback
void gameplay_update(void)
{
    if(g_Game.gameState != GAME_STATE_GAMEPLAY)
    {
        return;
    }

    // update stars even if game is paused
    updateStars();

    if(g_Game.isPaused == true)
    {
        return;
    }

    if(g_Game.isOnlineMode)
    {
        // Server controls wave/gameover/alien — but handle game-over countdown locally
        if(g_Game.isGameOver == true)
        {
            g_Game.gameOverFrames--;
            if(g_Game.gameOverFrames <= 0)
            {
                // Show the ranked score screen (same as offline game over)
                // Pause menu handles LOBBY vs RESTART based on isOnlineMode
                pauseGame(GAME_OVER_TRACK);
            }
            return;
        }

        updateDisasteroids();    // deterministic physics, same on all Saturns
        updateProjectiles();     // local cosmetic
        updateExplosions();
        updateShipDebris();
        updateGameplayPlayers(); // local ship physics

        // In online mode, only check projectile-asteroid and ship-ship collisions
        checkForDisasteroidCollisions();
        checkForPlayerCollisions();
    }
    else
    {
        // check for round and end game conditions
        isWaveOver();
        isGameOver();

        // game objects
        updateDisasteroids();
        updateProjectiles();
        updateAlienProjectiles();
        updateExplosions();
        updateShipDebris();
        updateGameplayPlayers();
        updateAlien();

        // collisions
        checkForAlienCollisions();
        checkForDisasteroidCollisions();
        checkForPlayerCollisions();
    }

    return;
}

// draw gameplay elements
void gameplay_draw(void)
{
    if(g_Game.gameState != GAME_STATE_GAMEPLAY)
    {
        return;
    }

    // draw stars even if game is paused
    drawStars();

    if(g_Game.isPaused == true)
    {
        return;
    }

    //
    // Draw objects from least important to most important
    //

    drawExplosions();
    drawShipDebris();
    drawProjectiles();
    drawAlienProjectiles();
    drawDisasteroids();
    drawAlien();
    drawGameplayWave();
    drawPlayers();

    return;
}

//
// Players
//

// poll each player
static void getGameplayPlayersInput(void)
{
    PPLAYER player = NULL;

    // check inputs for all players
    for(unsigned int i = 0; i < COUNTOF(g_Players); i++)
    {
        player = &g_Players[i];

        if(player->objectState != OBJECT_STATE_ACTIVE)
        {
            continue;
        }

        // don't check inputs if the player is respawning
        if(player->respawnFrames > 0)
        {
            continue;
        }

        // is player attempting to change color?
        if (jo_is_input_key_pressed(player->playerID, JO_KEY_X))
        {
            if(player->input.pressedX == false)
            {
                pushPlayerColor(player->color);
                player->color = popPlayerColor();
            }
            player->input.pressedX = true;
        }
        else
        {
            player->input.pressedX = false;
        }

        // Is player rotating?
        if (jo_is_input_key_pressed(player->playerID, JO_KEY_LEFT))
        {
            if(player->input.pressedLeft == false)
            {
                player->curPos.drot = -Z_SPEED_INC;
            }
        }
        else if (jo_is_input_key_pressed(player->playerID, JO_KEY_RIGHT))
        {
            if(player->input.pressedRight == false)
            {
                player->curPos.drot = Z_SPEED_INC;
            }
        }
        else
        {
            // no rotation
            player->curPos.drot = 0;
        }

        if(player->invulnerabilityFrames > INVULNERABILITY_TIMER/2)
        {
            continue;
        }

        // Is player shooting a projectile?
        if (jo_is_input_key_pressed(player->playerID, JO_KEY_A) ||
            jo_is_input_key_pressed(player->playerID, JO_KEY_C))
        {
            if(player->input.pressedAC == false)
            {
                spawnProjectile(player);
                player->input.pressedAC = true;
            }
        }
        else
        {
            player->input.pressedAC = false;
        }


        // Is player thrusting?
        if (jo_is_input_key_pressed(player->playerID, JO_KEY_UP) ||
            jo_is_input_key_pressed(player->playerID, JO_KEY_B))
        {

            player->curPos.dx += jo_fixed_sin(jo_fixed_mult(toFIXED(player->curPos.rot), JO_FIXED_PI_DIV_180));
            player->curPos.dy -= jo_fixed_cos(jo_fixed_mult(toFIXED(player->curPos.rot), JO_FIXED_PI_DIV_180));
            player->isThrusting = true;
        }
        else
        {
            // not thrusting, apply friction
            player->curPos.dx -= jo_fixed_mult(FRICTION, player->curPos.dx);
            player->curPos.dy -= jo_fixed_mult(FRICTION, player->curPos.dy);
            player->isThrusting = false;
        }


        // bound max speed
        if(player->curPos.dx > MAX_SPEED_X)
        {
            player->curPos.dx = MAX_SPEED_X;
        }
        else if(player->curPos.dx < MIN_SPEED_X)
        {
            player->curPos.dx = MIN_SPEED_X;
        }

        if(player->curPos.dy > MAX_SPEED_Y)
        {
            player->curPos.dy = MAX_SPEED_Y;
        }
        else if(player->curPos.dy < MIN_SPEED_Y)
        {
            player->curPos.dy = MIN_SPEED_Y;
        }
    }
}

// pack local controller input into a bitmask for network transmission
static uint16_t packLocalInput(int playerID)
{
    uint16_t bits = 0;
    if (jo_is_input_key_pressed(playerID, JO_KEY_UP))    bits |= DNET_INPUT_UP;
    if (jo_is_input_key_pressed(playerID, JO_KEY_DOWN))  bits |= DNET_INPUT_DOWN;
    if (jo_is_input_key_pressed(playerID, JO_KEY_LEFT))  bits |= DNET_INPUT_LEFT;
    if (jo_is_input_key_pressed(playerID, JO_KEY_RIGHT)) bits |= DNET_INPUT_RIGHT;
    if (jo_is_input_key_pressed(playerID, JO_KEY_A))     bits |= DNET_INPUT_A;
    if (jo_is_input_key_pressed(playerID, JO_KEY_B))     bits |= DNET_INPUT_B;
    if (jo_is_input_key_pressed(playerID, JO_KEY_C))     bits |= DNET_INPUT_C;
    if (jo_is_input_key_pressed(playerID, JO_KEY_X))     bits |= DNET_INPUT_X;
    if (jo_is_input_key_pressed(playerID, JO_KEY_START)) bits |= DNET_INPUT_START;
    return bits;
}

// apply a network input bitmask to a player (same logic as controller input)
// isLocal=false skips thrust/friction so remote ships coast on server velocity
static void applyInputBitsToPlayer(PPLAYER player, uint16_t bits, bool isLocal)
{
    if(player->objectState != OBJECT_STATE_ACTIVE) return;
    if(player->respawnFrames > 0) return;

    // Color change
    if (bits & DNET_INPUT_X) {
        if(player->input.pressedX == false) {
            pushPlayerColor(player->color);
            player->color = popPlayerColor();
        }
        player->input.pressedX = true;
    } else {
        player->input.pressedX = false;
    }

    // Rotation — local only. Remote rotation comes from SHIP_SYNC.
    if (isLocal) {
        if (bits & DNET_INPUT_LEFT) {
            if(player->input.pressedLeft == false) {
                player->curPos.drot = -Z_SPEED_INC;
            }
        } else if (bits & DNET_INPUT_RIGHT) {
            if(player->input.pressedRight == false) {
                player->curPos.drot = Z_SPEED_INC;
            }
        } else {
            player->curPos.drot = 0;
        }
    } else {
        // Remote: drot=0, rotation is snapped by SHIP_SYNC
        player->curPos.drot = 0;
    }

    // Track left/right press state
    player->input.pressedLeft = (bits & DNET_INPUT_LEFT) ? true : false;
    player->input.pressedRight = (bits & DNET_INPUT_RIGHT) ? true : false;

    if(player->invulnerabilityFrames > INVULNERABILITY_TIMER/2) return;

    // Shooting
    if ((bits & DNET_INPUT_A) || (bits & DNET_INPUT_C)) {
        if(player->input.pressedAC == false) {
            spawnProjectile(player);
            player->input.pressedAC = true;
        }
    } else {
        player->input.pressedAC = false;
    }

    // Thrusting — only apply velocity changes for local players.
    // Remote players get velocity from SHIP_SYNC; just update visual flag.
    if ((bits & DNET_INPUT_UP) || (bits & DNET_INPUT_B)) {
        if (isLocal) {
            player->curPos.dx += jo_fixed_sin(jo_fixed_mult(toFIXED(player->curPos.rot), JO_FIXED_PI_DIV_180));
            player->curPos.dy -= jo_fixed_cos(jo_fixed_mult(toFIXED(player->curPos.rot), JO_FIXED_PI_DIV_180));
        }
        player->isThrusting = true;
    } else {
        if (isLocal) {
            player->curPos.dx -= jo_fixed_mult(FRICTION, player->curPos.dx);
            player->curPos.dy -= jo_fixed_mult(FRICTION, player->curPos.dy);
        }
        player->isThrusting = false;
    }

    // Speed bounds (local only — remote velocity comes from server)
    if (isLocal) {
        if(player->curPos.dx > MAX_SPEED_X) player->curPos.dx = MAX_SPEED_X;
        else if(player->curPos.dx < MIN_SPEED_X) player->curPos.dx = MIN_SPEED_X;
        if(player->curPos.dy > MAX_SPEED_Y) player->curPos.dy = MAX_SPEED_Y;
        else if(player->curPos.dy < MIN_SPEED_Y) player->curPos.dy = MIN_SPEED_Y;
    }
}

// online mode input: local player sends inputs, all others receive from network
static void getOnlinePlayersInput(void)
{
    uint16_t local_bits;
    int remote_bits;
    uint16_t frame;
    unsigned int i;
    uint8_t my_id = g_Game.myPlayerID;

    frame = (uint16_t)(g_Game.netFrameCount & 0xFFFF);
    g_Game.netFrameCount++;

    // P2 controller hot-plug detection during gameplay
    if (g_Game.hasSecondLocal && getP2Port() < 0) {
        // Controller 2 unplugged mid-game
        g_Game.hasSecondLocal = false;
        dnet_send_remove_local_player();
        g_Game.myPlayerID2 = 0xFF;
    }

    for(i = 0; i < COUNTOF(g_Players); i++)
    {
        if(g_Players[i].objectState != OBJECT_STATE_ACTIVE)
            continue;

        if(i == (unsigned int)my_id) {
            // Local player 1: always read from controller port 0 (pad 1)
            local_bits = packLocalInput(0);
            applyInputBitsToPlayer(&g_Players[i], local_bits, true);
            dnet_send_input_delta(frame, local_bits);
            dnet_send_ship_state(); // throttled internally to every 10 frames
        } else if(g_Game.hasSecondLocal && i == (unsigned int)g_Game.myPlayerID2) {
            // Local player 2: read from second controller port
            local_bits = packLocalInput(getP2Port() >= 0 ? getP2Port() : 6);
            applyInputBitsToPlayer(&g_Players[i], local_bits, true);
            dnet_send_input_delta_p2(frame, local_bits);
            dnet_send_ship_state_p2();
        } else {
            // Remote player: read from network buffer
            remote_bits = dnet_get_remote_input(frame, (uint8_t)i);
            if(remote_bits >= 0) {
                applyInputBitsToPlayer(&g_Players[i], (uint16_t)remote_bits, false);
            } else {
                // No remote input yet — idle (no buttons)
                applyInputBitsToPlayer(&g_Players[i], 0, false);
            }
        }
    }
}

// update players
static void updateGameplayPlayers(void)
{
    PPLAYER player = NULL;

    for(unsigned int i = 0; i < COUNTOF(g_Players); i++)
    {
        player = &g_Players[i];

        if(player->objectState != OBJECT_STATE_ACTIVE)
        {
            continue;
        }

        // don't do anything if we are waiting to respawn
        if(player->respawnFrames > 0)
        {
            player->respawnFrames--;
            continue;
        }

        // update invulnerability frames
        if(player->invulnerabilityFrames > 0)
        {
            player->invulnerabilityFrames--;
        }

        // player position
        player->curPos.x += player->curPos.dx;
        player->curPos.y += player->curPos.dy;
        boundGameplayObject((PGAME_OBJECT)player);

        // to do move this to player update
        player->curPos.rot += player->curPos.drot;
        if(player->curPos.rot > DEGREES_IN_CIRCLE)
        {
            player->curPos.rot -= DEGREES_IN_CIRCLE;
        }
        else if(player->curPos.rot < -DEGREES_IN_CIRCLE)
        {
            player->curPos.rot += DEGREES_IN_CIRCLE;
        }
    }
}

//
// Gameplay State
//

// intialize game globals
static void initNewGame(void)
{
    g_Game.isPaused = false;
    g_Game.isGameOver = false;
    g_Game.wave = 0;
    g_Game.gameOverFrames = 0;
}

// start the next wave of disasteroids
static void initWave(void)
{
    g_Game.wave++;

    if(g_Game.wave > MAX_WAVE)
    {
        g_Game.wave = MAX_WAVE;
    }

    spawnPlayers();
    spawnDisasteroids();

    // spawn alien every 4 waves
    if((g_Game.wave & 0x3) == 0)
    {
        spawnAlien();
    }

    // remove all projectiles after every wave
    initProjectiles();

    initStars();

    if(g_Game.wave > 1)
    {
        // only strobe star on cleared waves
        strobeStars();

        playCDTrack(VICTORY_TRACK);
    }
}

// checks if the current wave of disasteroids is complete
static void isWaveOver(void)
{
    bool result = false;

    result = checkAliveDisasteroids();
    if(result == false)
    {
        // implement timer
        initWave();
        return;
    }

    return;
}

// checks if the game is over
static void isGameOver(void)
{
    int numPlayers = 0;

    //
    // check if we are waiting for the game over timer
    //
    if(g_Game.isGameOver == true)
    {
        g_Game.gameOverFrames--;
        if(g_Game.gameOverFrames <= 0)
        {
            pauseGame(GAME_OVER_TRACK);
        }

        return;
    }

    //
    // Check if the game is over
    //
    numPlayers = countAlivePlayers();

    if(g_Game.gameType == GAME_TYPE_COOP)
    {
        // COOP ends if all players are dead
        if(numPlayers == 0)
        {
            g_Game.isGameOver = true;
            g_Game.gameOverFrames = GAME_OVER_TIMER;
            return;
        }
    }
    else // versus mode
    {
        // versus mode ends when there's one player standing
        // check is for <= 1 in case both players kill each other
        // simultaneously
        if(numPlayers <= 1)
        {
            g_Game.isGameOver = true;
            g_Game.gameOverFrames = GAME_OVER_TIMER;
            return;
        }
    }

    // players are still alive
    return;
}

//
// Misc
//

// draws the current wave number on score
static void drawGameplayWave(void)
{
    int wave = 0;
    int color = 0;
    int xPos = 0;
    int yPos = 0;
    int xScale = 2;
    int yScale = 2;
    int letterSpacing = 6;
    unsigned char digit1 = 0;
    unsigned char digit2 = 0;

    color = g_Game.hudColor;

    wave = g_Game.wave;

    yPos = -80;
    xPos = -4;

    digit1 = (wave / 10) + '0';
    digit2 = (wave % 10) + '0';

    drawLetter(digit1, color, xPos + (letterSpacing * -1), yPos, xScale, yScale);
    drawLetter(digit2, color, xPos + (letterSpacing * 1), yPos, xScale, yScale);
}
