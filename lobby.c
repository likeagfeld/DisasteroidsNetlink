/**
 * lobby.c - Online Lobby Screen
 *
 * Shows up to 12 connected players with ready states, and handles game start.
 * Network messages are processed by dnet_tick() in the main loop.
 */

#include <jo/jo.h>
#include "main.h"
#include "lobby.h"
#include "assets/assets.h"
#include "net/disasteroids_net.h"
#include "objects/star.h"

/*============================================================================
 * Callbacks
 *============================================================================*/

void lobby_init(void)
{
    initStars();
}

void lobby_input(void)
{
    if (g_Game.gameState != GAME_STATE_LOBBY) return;

    /* A/C = toggle ready */
    if (jo_is_pad1_key_pressed(JO_KEY_A) ||
        jo_is_pad1_key_pressed(JO_KEY_C)) {
        if (g_Game.input.pressedAC == false) {
            dnet_send_ready();
        }
        g_Game.input.pressedAC = true;
    } else {
        g_Game.input.pressedAC = false;
    }

    /* START = request game start */
    if (jo_is_pad1_key_pressed(JO_KEY_START)) {
        if (g_Game.input.pressedStart == false) {
            dnet_send_start_game();
        }
        g_Game.input.pressedStart = true;
    } else {
        g_Game.input.pressedStart = false;
    }

    /* B = disconnect and return to title */
    if (jo_is_pad1_key_pressed(JO_KEY_B)) {
        if (g_Game.input.pressedB == false) {
            dnet_send_disconnect();
            transitionState(GAME_STATE_TITLE_SCREEN);
        }
        g_Game.input.pressedB = true;
    } else {
        g_Game.input.pressedB = false;
    }
}

void lobby_update(void)
{
    if (g_Game.gameState != GAME_STATE_LOBBY) return;

    updateStars();

    /* Network tick is done in main loop, but check state transitions */
    if (dnet_get_state() == DNET_STATE_PLAYING) {
        const dnet_state_data_t* nd = dnet_get_data();

        /* Configure game from server settings */
        g_Game.isOnlineMode = true;
        g_Game.myPlayerID = nd->my_player_id;
        g_Game.gameType = nd->game_type;
        g_Game.numLives = nd->num_lives > 0 ? nd->num_lives : 3;

        transitionState(GAME_STATE_GAMEPLAY);
    }

    if (dnet_get_state() == DNET_STATE_DISCONNECTED) {
        transitionState(GAME_STATE_TITLE_SCREEN);
    }
}

void lobby_draw(void)
{
    const dnet_state_data_t* nd;
    int color;
    int yPos;
    int i;

    if (g_Game.gameState != GAME_STATE_LOBBY) return;

    drawStars();

    nd = dnet_get_data();
    color = g_Game.hudColor;

    /* Title: LOBBY */
    yPos = -60;
    drawLetter('L', color, -27, yPos, 3, 3);
    drawLetter('O', color, -11, yPos, 3, 3);
    drawLetter('B', color,   5, yPos, 3, 3);
    drawLetter('B', color,  21, yPos, 3, 3);
    drawLetter('Y', color,  37, yPos, 3, 3);

    /* Player count */
    jo_printf(2, 8, "Players: %d/%d", nd->lobby_count, DNET_MAX_PLAYERS);

    /* Player list - compact rows to fit up to 12 */
    for (i = 0; i < nd->lobby_count && i < DNET_MAX_PLAYERS; i++) {
        int row = 10 + i;
        const char* name = nd->lobby_players[i].name;
        const char* ready_str = nd->lobby_players[i].ready ? "READY" : "---";

        jo_printf(3, row, "%-16s %s", name, ready_str);
    }

    /* Clear remaining rows if fewer players than last draw */
    for (; i < DNET_MAX_PLAYERS; i++) {
        int row = 10 + i;
        jo_printf(3, row, "                       ");
    }

    /* Waiting indicator */
    if (nd->lobby_count < 2) {
        jo_printf(5, 24, "Waiting for players...");
    } else {
        jo_printf(5, 24, "                      ");
    }

    /* Log lines */
    for (i = 0; i < nd->log_count && i < 2; i++) {
        jo_printf(3, 25 + i, "%s", nd->log_lines[i]);
    }

    /* Controls hint */
    jo_printf(2, 27, "A:Ready  START:Go  B:Quit");
}
