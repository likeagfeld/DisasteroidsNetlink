/**
 * connecting.c - Connection Screen for Online Play
 *
 * Manages the modem connection flow: probing, initializing, dialing.
 * Uses a frame-by-frame state machine so status messages render
 * between blocking modem calls (same pattern as Coup's render_and_sync).
 */

#include <jo/jo.h>
#include "main.h"
#include "connecting.h"
#include "assets/assets.h"
#include "net/disasteroids_net.h"
#include "net/saturn_uart16550.h"
#include "net/modem.h"
#include "objects/star.h"

/* slSynch() forces a frame render before blocking modem calls */
extern void slSynch(void);

/*============================================================================
 * Configuration
 *============================================================================*/

#define CONNECT_DIAL_NUMBER   "778"
#define CONNECT_DIAL_TIMEOUT  180000000  /* ~60 seconds at 28.6MHz */

/*============================================================================
 * Saturn UART + Transport (defined in main.c, externed here)
 *============================================================================*/

extern saturn_uart16550_t g_uart;
extern bool g_modem_detected;
extern net_transport_t g_saturn_transport;

/*============================================================================
 * Connection State Machine
 *
 * Each stage sets the status text, returns to the main loop (so the draw
 * callback renders the message), then the NEXT frame executes the blocking
 * modem call and advances to the following stage.
 *============================================================================*/

typedef enum {
    CONNECT_STAGE_INIT = 0,
    CONNECT_STAGE_SHOW_PROBE,    /* Display "Probing modem..." */
    CONNECT_STAGE_PROBING,       /* Execute modem_probe() */
    CONNECT_STAGE_SHOW_INIT,     /* Display "Initializing modem..." */
    CONNECT_STAGE_MODEM_INIT,    /* Execute modem_init() */
    CONNECT_STAGE_SHOW_DIAL,     /* Display "Dialing server..." */
    CONNECT_STAGE_DIALING,       /* Execute modem_dial() */
    CONNECT_STAGE_CONNECTED,     /* Success - transition to lobby */
    CONNECT_STAGE_FAILED,        /* Failure - wait then return to title */
} connect_stage_t;

static connect_stage_t g_connect_stage;
static const char* g_connect_msg = "";
static int g_connect_timer = 0;

/*============================================================================
 * Callbacks
 *============================================================================*/

void connecting_init(void)
{
    g_connect_stage = CONNECT_STAGE_INIT;
    g_connect_msg = "Preparing...";
    g_connect_timer = 0;

    dnet_init();
    dnet_set_modem_available(g_modem_detected);
    dnet_set_username(g_Game.playerName[0] ? g_Game.playerName : "PLAYER");

    /* Re-detect second controller (may have been plugged in after name entry) */
    if (!g_Game.hasSecondLocal && getP2Port() >= 0) {
        int i, p2len;
        g_Game.hasSecondLocal = true;
        /* Auto-generate playerName2 = playerName + "2" */
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
    }
    g_Game.myPlayerID2 = 0xFF;

    initStars();
}

void connecting_input(void)
{
    if (g_Game.gameState != GAME_STATE_CONNECTING) return;

    /* B button to cancel and return to title */
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

void connecting_update(void)
{
    modem_result_t result;

    if (g_Game.gameState != GAME_STATE_CONNECTING) return;

    updateStars();

    switch (g_connect_stage) {

    case CONNECT_STAGE_INIT:
        if (!g_modem_detected) {
            g_connect_msg = "No NetLink modem";
            dnet_log("No NetLink modem detected");
            g_connect_stage = CONNECT_STAGE_FAILED;
            return;
        }
        /* Advance to show probe message next frame */
        g_connect_stage = CONNECT_STAGE_SHOW_PROBE;
        break;

    case CONNECT_STAGE_SHOW_PROBE:
        /* Set message and let it render for one frame */
        g_connect_msg = "Probing modem...";
        dnet_log("Probing modem...");
        g_connect_stage = CONNECT_STAGE_PROBING;
        break;

    case CONNECT_STAGE_PROBING:
        /* Force frame render right before blocking call */
        slSynch();

        if (modem_probe(&g_uart) != MODEM_OK) {
            g_connect_msg = "No modem response";
            dnet_log("No modem response");
            g_connect_stage = CONNECT_STAGE_FAILED;
            return;
        }
        dnet_log("Modem detected");
        g_connect_stage = CONNECT_STAGE_SHOW_INIT;
        break;

    case CONNECT_STAGE_SHOW_INIT:
        g_connect_msg = "Initializing modem...";
        dnet_log("Initializing modem...");
        g_connect_stage = CONNECT_STAGE_MODEM_INIT;
        break;

    case CONNECT_STAGE_MODEM_INIT:
        slSynch();

        if (modem_init(&g_uart) != MODEM_OK) {
            g_connect_msg = "Modem init failed";
            dnet_log("Modem init failed");
            g_connect_stage = CONNECT_STAGE_FAILED;
            return;
        }
        dnet_log("Modem ready");
        g_connect_stage = CONNECT_STAGE_SHOW_DIAL;
        break;

    case CONNECT_STAGE_SHOW_DIAL:
        g_connect_msg = "Dialing server...";
        dnet_log("Dialing " CONNECT_DIAL_NUMBER "...");
        g_connect_stage = CONNECT_STAGE_DIALING;
        break;

    case CONNECT_STAGE_DIALING:
        slSynch();

        result = modem_dial(&g_uart, CONNECT_DIAL_NUMBER, CONNECT_DIAL_TIMEOUT);
        switch (result) {
        case MODEM_CONNECT:
            g_connect_msg = "Connected!";
            dnet_log("Connection established!");
            modem_flush_input(&g_uart);
            g_connect_stage = CONNECT_STAGE_CONNECTED;
            break;
        case MODEM_NO_CARRIER:
            g_connect_msg = "NO CARRIER";
            dnet_log("NO CARRIER - Check cable");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        case MODEM_BUSY:
            g_connect_msg = "LINE BUSY";
            dnet_log("LINE BUSY - Try again");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        case MODEM_NO_DIALTONE:
            g_connect_msg = "NO DIALTONE";
            dnet_log("NO DIALTONE - Check line");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        case MODEM_NO_ANSWER:
            g_connect_msg = "NO ANSWER";
            dnet_log("NO ANSWER - Server down?");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        case MODEM_TIMEOUT_ERR:
            g_connect_msg = "TIMEOUT";
            dnet_log("TIMEOUT - Server offline?");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        default:
            g_connect_msg = "Unknown error";
            dnet_log("Dial failed");
            g_connect_stage = CONNECT_STAGE_FAILED;
            break;
        }
        break;

    case CONNECT_STAGE_CONNECTED:
        /* Reset RX FIFO after connect */
        saturn_uart_reg_write(&g_uart, SATURN_UART_FCR,
            SATURN_UART_FCR_ENABLE | SATURN_UART_FCR_RXRESET);
        dnet_set_transport(&g_saturn_transport);
        dnet_on_connected();
        transitionState(GAME_STATE_LOBBY);
        break;

    case CONNECT_STAGE_FAILED:
        g_connect_timer++;
        if (g_connect_timer > 180) { /* 3 seconds */
            transitionState(GAME_STATE_TITLE_SCREEN);
        }
        break;
    }
}

void connecting_draw(void)
{
    int color;
    int yPos;

    if (g_Game.gameState != GAME_STATE_CONNECTING) return;

    drawStars();

    color = g_Game.hudColor;
    yPos = -30;

    /* Title */
    drawLetter('C', color, -55, yPos, 2, 2);
    drawLetter('O', color, -44, yPos, 2, 2);
    drawLetter('N', color, -33, yPos, 2, 2);
    drawLetter('N', color, -22, yPos, 2, 2);
    drawLetter('E', color, -11, yPos, 2, 2);
    drawLetter('C', color,   0, yPos, 2, 2);
    drawLetter('T', color,  11, yPos, 2, 2);
    drawLetter('I', color,  22, yPos, 2, 2);
    drawLetter('N', color,  33, yPos, 2, 2);
    drawLetter('G', color,  44, yPos, 2, 2);

    /* Status message via jo_printf (padded to clear previous longer text) */
    jo_printf(5, 17, "%-25s", g_connect_msg);

    /* Log lines (padded to clear stale text) */
    {
        const dnet_state_data_t* nd = dnet_get_data();
        int i;
        for (i = 0; i < 4; i++) {
            if (i < nd->log_count) {
                jo_printf(3, 19 + i, "%-33s", nd->log_lines[i]);
            } else {
                jo_printf(3, 19 + i, "                                 ");
            }
        }
    }

    /* Cancel hint */
    jo_printf(8, 26, "Press B to cancel");
}
