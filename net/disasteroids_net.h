/**
 * disasteroids_net.h - Disasteroids Networking State Machine
 *
 * Manages the full network lifecycle: modem detection, connection,
 * authentication, lobby, and in-game input relay.
 *
 * Architecture follows the Coup example exactly:
 *   Saturn + NetLink --phone--> USB Modem --serial--> Bridge --TCP--> Server
 */

#ifndef DISASTEROIDS_NET_H
#define DISASTEROIDS_NET_H

#include <stdint.h>
#include <stdbool.h>
#include "net_transport.h"
#include "disasteroids_protocol.h"

/*============================================================================
 * Constants
 *============================================================================*/

#define DNET_MAX_PLAYERS       12    /* Online mode: up to 12 players */
#define DNET_MAX_NAME          16
#define DNET_HEARTBEAT_INTERVAL 600  /* frames (~10 sec at 60fps) */
#define DNET_AUTH_TIMEOUT       300  /* frames (~5 sec) */
#define DNET_AUTH_MAX_RETRIES   5
#define DNET_MAX_PACKETS_FRAME  24   /* Max packets to process per frame (11 opponents + overhead) */
#define DNET_INPUT_BUFFER_PER_PLAYER 8  /* Frames of input to buffer per player */

/*============================================================================
 * Network State Machine
 *============================================================================*/

typedef enum {
    DNET_STATE_OFFLINE = 0,     /* No network, local play only */
    DNET_STATE_CONNECTING,      /* Modem dialing */
    DNET_STATE_AUTHENTICATING,  /* Sent CONNECT, waiting for WELCOME */
    DNET_STATE_USERNAME,        /* Server requested username */
    DNET_STATE_LOBBY,           /* In lobby, waiting for players */
    DNET_STATE_PLAYING,         /* In-game, relaying inputs */
    DNET_STATE_DISCONNECTED,    /* Connection lost */
} dnet_state_t;

/*============================================================================
 * Lobby Player Info
 *============================================================================*/

typedef struct {
    uint8_t id;
    char    name[DNET_MAX_NAME + 1];
    bool    ready;
    bool    active;
} dnet_lobby_player_t;

/*============================================================================
 * Remote Input Buffer
 *
 * Stores incoming input states from the remote player.
 * The game reads from this buffer to apply remote inputs.
 *============================================================================*/

typedef struct {
    uint16_t frame_num;
    uint16_t input_bits;
    uint8_t  player_id;
    bool     valid;
} dnet_input_entry_t;

/*============================================================================
 * Network State
 *============================================================================*/

typedef struct {
    /* Connection state */
    dnet_state_t state;
    bool modem_available;

    /* Transport */
    const net_transport_t* transport;

    /* RX state machine */
    dnet_rx_state_t rx;
    uint8_t rx_buf[DNET_RX_FRAME_SIZE];
    uint8_t tx_buf[DNET_TX_FRAME_SIZE];

    /* Auth */
    char my_uuid[SNCP_UUID_LEN + 4];
    bool has_uuid;
    uint8_t my_player_id;
    int auth_timer;
    int auth_retries;

    /* Username */
    char my_name[DNET_MAX_NAME + 1];

    /* Lobby */
    dnet_lobby_player_t lobby_players[DNET_MAX_PLAYERS];
    int lobby_count;
    bool my_ready;

    /* Game config (from server GAME_START) */
    uint32_t game_seed;
    uint8_t  game_type;
    uint8_t  num_lives;
    uint8_t  opponent_count;

    /* In-game input relay — per-player ring buffers */
    dnet_input_entry_t remote_inputs[DNET_MAX_PLAYERS][DNET_INPUT_BUFFER_PER_PLAYER];
    int remote_input_head[DNET_MAX_PLAYERS];
    uint16_t local_frame;

    /* Username retry (for duplicate name handling) */
    int username_retry;

    /* Delta compression */
    uint16_t last_sent_input;   /* Last input bits sent to server */
    uint16_t send_cooldown;     /* Frames since last send (force at 15) */

    /* Timers */
    int heartbeat_counter;
    int frame_count;

    /* Connection status messages */
    const char* status_msg;
    int connect_stage;

    /* Log messages for display */
    char log_lines[4][40];
    int  log_count;

} dnet_state_data_t;

/*============================================================================
 * Public API
 *============================================================================*/

/** Initialize network state (call once at startup). */
void dnet_init(void);

/** Set modem availability (call after hardware detection). */
void dnet_set_modem_available(bool available);

/** Set the transport (call after successful modem connection). */
void dnet_set_transport(const net_transport_t* transport);

/** Set username for online play. */
void dnet_set_username(const char* name);

/** Get current network state. */
dnet_state_t dnet_get_state(void);

/** Get pointer to full state (for rendering). */
const dnet_state_data_t* dnet_get_data(void);

/**
 * Called when modem connection established.
 * Sends CONNECT message to server, transitions to AUTHENTICATING.
 */
void dnet_on_connected(void);

/**
 * Network tick — call every frame.
 * Polls for incoming messages, processes them, sends heartbeat.
 */
void dnet_tick(void);

/**
 * Send local player input to server (call every frame during gameplay).
 */
void dnet_send_input(uint16_t frame_num, uint16_t input_bits);

/**
 * Send local player input with delta compression.
 * Only transmits when input changes or every 15 frames as keepalive.
 */
void dnet_send_input_delta(uint16_t frame_num, uint16_t input_bits);

/**
 * Check if remote input is available for the given frame and player.
 * Returns the input bits, or -1 if not yet received.
 */
int dnet_get_remote_input(uint16_t frame_num, uint8_t player_id);

/** Toggle ready state in lobby. */
void dnet_send_ready(void);

/** Request game start (from lobby). */
void dnet_send_start_game(void);

/** Send disconnect and clean up. */
void dnet_send_disconnect(void);

/** Request pause toggle. */
void dnet_send_pause(void);

/** Add a log message (visible on connecting/lobby screens). */
void dnet_log(const char* msg);

/** Enter offline mode (no modem or connection failed). */
void dnet_enter_offline(void);

#endif /* DISASTEROIDS_NET_H */
