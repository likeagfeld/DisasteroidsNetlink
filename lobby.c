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

/*============================================================================
 * Z-button overlay state
 *============================================================================*/

static bool g_z_held = false;        /* Is Z currently held */
static bool g_z_was_held = false;    /* Was Z held last frame (for release detect) */
static int  g_z_page_timer = 0;     /* Frame counter for page alternation */
static int  g_z_page = 0;           /* 0 = last results, 1 = leaderboard */
#define Z_PAGE_INTERVAL 180          /* 3 seconds at 60fps */

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
    int row;

    g_bot_difficulty = 1;
    g_z_held = false;
    g_z_was_held = false;
    g_z_page_timer = 0;
    g_z_page = 0;

    /* Clear stale log text from previous gameplay */
    dnet_clear_log();

    /* Blank VDP2 text rows that might have residual text from prior screens */
    for (row = 8; row <= 27; row++) {
        jo_printf(0, row, "                                        ");
    }

    /* Request leaderboard data from server */
    dnet_request_leaderboard();

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

    /* Z = hold for results/leaderboard overlay */
    g_z_held = jo_is_pad1_key_pressed(JO_KEY_Z) ? true : false;
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

/* Look up a player name by game_player_id from game roster */
static const char* lobbyGetPlayerName(int id)
{
    const dnet_state_data_t* nd = dnet_get_data();
    int i;
    for (i = 0; i < nd->game_roster_count && i < DNET_MAX_PLAYERS; i++) {
        if (nd->game_roster[i].active && nd->game_roster[i].id == (uint8_t)id)
            return nd->game_roster[i].name;
    }
    return "";
}

/* Get the winner's name from game roster using last_winner_id (game_player_id) */
static const char* getWinnerName(void)
{
    const dnet_state_data_t* nd = dnet_get_data();
    int i;
    if (!nd->has_last_results || nd->last_winner_id == 0xFF)
        return "";
    for (i = 0; i < nd->game_roster_count && i < DNET_MAX_PLAYERS; i++) {
        if (nd->game_roster[i].active && nd->game_roster[i].id == nd->last_winner_id)
            return nd->game_roster[i].name;
    }
    return "";
}

/* Draw Z-overlay: results or leaderboard */
static void draw_z_overlay(const dnet_state_data_t* nd)
{
    int i, row;

    /* Advance page timer */
    g_z_page_timer++;
    if (g_z_page_timer >= Z_PAGE_INTERVAL) {
        g_z_page_timer = 0;
        g_z_page = 1 - g_z_page; /* toggle 0/1 */
    }

    /* Clear overlay area */
    for (row = 8; row <= 22; row++) {
        jo_printf(0, row, "                                        ");
    }

    if (g_z_page == 0 && nd->has_last_results) {
        /* Page 0: Last Game Results */
        /* Build list from game roster, sort by score descending */
        int indices[MAX_PLAYERS];
        int count = 0;

        for (i = 0; i < nd->game_roster_count && i < DNET_MAX_PLAYERS; i++) {
            if (nd->game_roster[i].active) {
                int pid = nd->game_roster[i].id;
                if (pid < MAX_PLAYERS) {
                    indices[count++] = pid;
                }
            }
        }

        /* Simple insertion sort by score descending */
        for (i = 1; i < count; i++) {
            int j = i;
            while (j > 0 && g_Players[indices[j]].score.points >
                            g_Players[indices[j-1]].score.points) {
                int tmp = indices[j];
                indices[j] = indices[j-1];
                indices[j-1] = tmp;
                j--;
            }
        }

        jo_printf(6, 8, "LAST GAME RESULTS");
        jo_printf(2, 9, "#  NAME             PTS  LIV");
        for (i = 0; i < count && i < 12; i++) {
            int pid = indices[i];
            const char* name = lobbyGetPlayerName(pid);
            if (name[0] == '\0') name = "???";
            jo_printf(2, 10 + i, "%-2d %-16s %4d  %d",
                      i + 1, name,
                      g_Players[pid].score.points % 10000,
                      g_Players[pid].numLives);
        }
    } else if (g_z_page == 1) {
        /* Page 1: Online Leaderboard */
        if (nd->leaderboard_count > 0) {
            jo_printf(5, 8, "ONLINE LEADERBOARD");
            jo_printf(2, 9, "#  NAME             W  SCR  GP");
            for (i = 0; i < nd->leaderboard_count && i < 12; i++) {
                jo_printf(2, 10 + i, "%-2d %-16s %2d %4d %3d",
                          i + 1,
                          nd->leaderboard[i].name,
                          nd->leaderboard[i].wins,
                          nd->leaderboard[i].best_score % 10000,
                          nd->leaderboard[i].games_played % 1000);
            }
        } else {
            jo_printf(5, 8, "ONLINE LEADERBOARD");
            jo_printf(6, 14, "No data yet");
        }
    } else {
        /* Page 0 but no last results — show leaderboard instead */
        if (nd->leaderboard_count > 0) {
            jo_printf(5, 8, "ONLINE LEADERBOARD");
            jo_printf(2, 9, "#  NAME             W  SCR  GP");
            for (i = 0; i < nd->leaderboard_count && i < 12; i++) {
                jo_printf(2, 10 + i, "%-2d %-16s %2d %4d %3d",
                          i + 1,
                          nd->leaderboard[i].name,
                          nd->leaderboard[i].wins,
                          nd->leaderboard[i].best_score % 10000,
                          nd->leaderboard[i].games_played % 1000);
            }
        } else {
            jo_printf(6, 14, "No data yet");
        }
    }

    /* Page indicator */
    if (nd->has_last_results) {
        jo_printf(2, 22, "Z: %s", g_z_page == 0 ? "RESULTS " : "LEADERS ");
    } else {
        jo_printf(2, 22, "Z: LEADERS ");
    }
}

/* Clear the Z-overlay area when released */
static void clear_z_overlay(void)
{
    int row;
    for (row = 8; row <= 22; row++) {
        jo_printf(0, row, "                                        ");
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

    /* Check if Z overlay is active — if so, draw that instead of player list */
    if (g_z_held) {
        draw_z_overlay(nd);
        g_z_was_held = true;
        /* Skip normal player list drawing */
        goto skip_player_list;
    } else if (g_z_was_held) {
        /* Z was just released — clear overlay and redraw player list */
        clear_z_overlay();
        g_z_was_held = false;
        g_z_page_timer = 0;
        g_z_page = 0;
    }

    /* Player list - compact rows to fit up to 12 */
    {
        const char* winner_name = getWinnerName();

        for (i = 0; i < nd->lobby_count && i < DNET_MAX_PLAYERS; i++) {
            int row = 10 + i;
            const char* name = nd->lobby_players[i].name;
            const char* ready_str = nd->lobby_players[i].ready ? "READY" : "---  ";

            jo_printf(3, row, "%-16s %-5s", name, ready_str);

            /* Show WIN! next to the last game's winner — match by name since
             * lobby id (user_id / 200+bot_id) differs from game_player_id */
            if (winner_name[0] != '\0' && strcmp(name, winner_name) == 0) {
                jo_printf(26, row, "WIN!");
            } else {
                jo_printf(26, row, "    ");
            }
        }
    }

    /* Clear remaining rows if fewer players than last draw */
    for (; i < DNET_MAX_PLAYERS; i++) {
        int row = 10 + i;
        jo_printf(3, row, "                              ");
    }

skip_player_list:

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
    jo_printf(1, 27, "A:Rdy START:Go B:Back Z:Stats");
}
