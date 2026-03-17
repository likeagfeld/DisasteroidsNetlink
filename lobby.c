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
 * Bot difficulty
 *============================================================================*/

static int g_bot_difficulty = 1;  /* 0=easy, 1=medium, 2=hard */

static const char* difficultyName(int d)
{
    switch(d) {
        case 0: return "EASY";
        case 1: return "MEDIUM";
        case 2: return "HARD";
        default: return "?";
    }
}

/*============================================================================
 * Callbacks
 *============================================================================*/

void lobby_init(void)
{
    g_bot_difficulty = 1;
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

    /* B = return to title (stay connected for quick rejoin) */
    if (jo_is_pad1_key_pressed(JO_KEY_B)) {
        if (g_Game.input.pressedB == false) {
            transitionState(GAME_STATE_TITLE_SCREEN);
        }
        g_Game.input.pressedB = true;
    } else {
        g_Game.input.pressedB = false;
    }

    /* Y = fully disconnect and return to title */
    if (jo_is_pad1_key_pressed(JO_KEY_Y)) {
        if (g_Game.input.pressedY == false) {
            dnet_send_disconnect();
            transitionState(GAME_STATE_TITLE_SCREEN);
        }
        g_Game.input.pressedY = true;
    } else {
        g_Game.input.pressedY = false;
    }

    /* L trigger = add bot */
    if (jo_is_pad1_key_pressed(JO_KEY_L)) {
        if (g_Game.input.pressedLT == false) {
            dnet_send_add_bot((uint8_t)g_bot_difficulty);
        }
        g_Game.input.pressedLT = true;
    } else {
        g_Game.input.pressedLT = false;
    }

    /* R trigger = remove bot */
    if (jo_is_pad1_key_pressed(JO_KEY_R)) {
        if (g_Game.input.pressedRT == false) {
            dnet_send_remove_bot();
        }
        g_Game.input.pressedRT = true;
    } else {
        g_Game.input.pressedRT = false;
    }

    /* X = cycle bot difficulty */
    if (jo_is_pad1_key_pressed(JO_KEY_X)) {
        if (g_Game.input.pressedX == false) {
            g_bot_difficulty++;
            if (g_bot_difficulty > 2) g_bot_difficulty = 0;
        }
        g_Game.input.pressedX = true;
    } else {
        g_Game.input.pressedX = false;
    }
}

void lobby_update(void)
{
    if (g_Game.gameState != GAME_STATE_LOBBY) return;

    updateStars();

    /* P2 controller hot-plug detection */
    if (!g_Game.hasSecondLocal && getP2Port() >= 0) {
        /* Controller 2 just plugged in — register P2 */
        int i, p2len;
        g_Game.hasSecondLocal = true;
        p2len = 0;
        while (g_Game.playerName[p2len] && p2len < DNET_MAX_NAME) p2len++;
        for (i = 0; i < p2len; i++)
            g_Game.playerName2[i] = g_Game.playerName[i];
        if (p2len < DNET_MAX_NAME) {
            g_Game.playerName2[p2len] = '2';
            g_Game.playerName2[p2len + 1] = '\0';
        } else {
            g_Game.playerName2[DNET_MAX_NAME - 1] = '2';
            g_Game.playerName2[DNET_MAX_NAME] = '\0';
        }
        g_Game.myPlayerID2 = 0xFF;
        dnet_send_add_local_player(g_Game.playerName2);
    } else if (g_Game.hasSecondLocal && getP2Port() < 0) {
        /* Controller 2 unplugged — remove P2 */
        g_Game.hasSecondLocal = false;
        g_Game.myPlayerID2 = 0xFF;
        g_Game.playerName2[0] = '\0';
        dnet_send_remove_local_player();
    }

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

        jo_printf(3, row, "%-16s %-5s", name, ready_str);
    }

    /* Clear remaining rows if fewer players than last draw */
    for (; i < DNET_MAX_PLAYERS; i++) {
        int row = 10 + i;
        jo_printf(3, row, "                       ");
    }

    /* Waiting indicator */
    if (nd->lobby_count < 2) {
        jo_printf(5, 23, "Waiting for players...");
    } else {
        jo_printf(5, 23, "                      ");
    }

    /* Log line (1 most recent, padded to clear stale text) */
    if (nd->log_count > 0) {
        jo_printf(3, 24, "%-35s", nd->log_lines[nd->log_count - 1]);
    } else {
        jo_printf(3, 24, "                                   ");
    }

    /* Show P2 status if second local player detected */
    if (g_Game.hasSecondLocal) {
        jo_printf(2, 25, "P2: %-16s", g_Game.playerName2);
    } else {
        jo_printf(2, 25, "                     ");
    }

    /* Bot difficulty + add/remove controls */
    jo_printf(1, 26, "Bot:%-6s X:Diff L:+ R:-", difficultyName(g_bot_difficulty));

    /* Controls hint */
    jo_printf(1, 27, "A:Rdy START:Go B:Back Y:Quit");
}
