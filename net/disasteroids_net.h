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
#define DNET_LEADERBOARD_MAX   10    /* Max leaderboard entries */

/* Snapshot ring (RING mode only). Per-pid history of received server poses,
 * indexed by server_frame. Size 20 covers ~2.6 sec at 7.5 Hz update rate
 * — enough headroom to bracket interp targets even under modem hiccups.
 *
 * DNET_INTERP_LAG_FRAMES is how far behind the latest snapshot we render,
 * tuned for 30 fps NTSC: 3 frames ≈ 100 ms (matches Utenyaa's wall-clock
 * window of 100 ms @ 50 fps PAL = 5 frames). Smaller = more responsive but
 * more visible extrapolation; larger = smoother but laggier. */
#define DNET_SNAP_RING_SIZE        20
#define DNET_INTERP_LAG_FRAMES      3

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
 * Game Roster (survives lobby state transition for results screen)
 *============================================================================*/

typedef struct {
    uint8_t id;                       /* game_player_id */
    char    name[DNET_MAX_NAME + 1];
    bool    active;
} dnet_roster_entry_t;

/*============================================================================
 * Leaderboard Entry
 *============================================================================*/

typedef struct {
    char     name[DNET_MAX_NAME + 1];
    uint16_t wins;
    uint16_t best_score;
    uint16_t games_played;
} dnet_leaderboard_entry_t;

/*============================================================================
 * Snapshot Ring Entry (RING sync mode)
 *
 * Single received server pose for a remote player, used by the
 * bracketing-pair interpolator. `frame` is the server's monotonic tick
 * counter at the moment the pose was sampled. Velocity is carried
 * alongside position so cheap extrapolation can run when no `newer`
 * snapshot has arrived yet.
 *============================================================================*/

typedef struct {
    uint16_t frame;     /* server frame_num when this snapshot was sent */
    int32_t  x, y;      /* position fxp 16.16 (decoded from quantized form) */
    int32_t  dx, dy;    /* velocity fxp 16.16 (decoded from quantized form) */
    int16_t  rot;       /* SGL angle */
    uint8_t  flags;     /* bit0=alive, bit1=invuln, bit2=thrust */
    uint8_t  valid;
} dnet_snap_entry_t;

typedef struct {
    dnet_snap_entry_t entries[DNET_SNAP_RING_SIZE];
    int      head;      /* next write position */
    int      count;     /* total entries written (saturating at SIZE) */
} dnet_snap_ring_t;

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

    /* Game roster (populated from PLAYER_JOIN, survives game-over for results screen) */
    dnet_roster_entry_t game_roster[DNET_MAX_PLAYERS];
    int game_roster_count;

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

    /* Ship state sync */
    int ship_state_cooldown;    /* Frames since last SHIP_STATE sent */

    /* Second local player (dual controller) */
    uint16_t last_sent_input_p2;
    uint16_t send_cooldown_p2;
    int ship_state_cooldown_p2;

    /* Server-authoritative mode */
    bool server_auth_mode;      /* True when server controls asteroids */

    /* Timers */
    int heartbeat_counter;
    int frame_count;

    /* Connection status messages */
    const char* status_msg;
    int connect_stage;

    /* Log messages for display */
    char log_lines[4][40];
    int  log_count;

    /* Last game winner */
    uint8_t last_winner_id;    /* Player ID of last game's winner (0xFF = none/coop) */
    bool    has_last_results;  /* True if we have results from a completed game */

    /* Online leaderboard (from server) */
    dnet_leaderboard_entry_t leaderboard[DNET_LEADERBOARD_MAX];
    int leaderboard_count;

    /* === Sync engine state (Phase 3) === */
    /* DNET_SYNC_MODE_LERP (0) = current single-snapshot lerp+extrap path;
     * DNET_SYNC_MODE_RING (1) = snapshot-ring interp/extrap path. Set by
     * server via DNET_MSG_SET_SYNC_MODE. Defaults to LERP — old servers
     * that never send the message keep us on the known-good path. */
    uint8_t  sync_mode;
    /* Most recently observed server frame number (from SHIP_SYNC_Q). Sent
     * back as `firer_frame` in PvP hit reports so the server can rewind
     * the victim to the moment we believed we landed the shot. */
    uint16_t last_recv_server_frame;
    /* True after we have sent CLIENT_CAPS at least once this connection. */
    bool     caps_sent;
    /* Per-pid snapshot rings. Indexed by player_id; my_player_id slot
     * is unused (we never render ourselves from a snapshot). */
    dnet_snap_ring_t snap_rings[DNET_MAX_PLAYERS];

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

/** Send local ship state to server (throttled to every 10 frames). */
void dnet_send_ship_state(void);

/** Send asteroid hit request to server. */
void dnet_send_asteroid_hit(uint8_t slot, uint8_t scorer_id);

/** Send ship-asteroid collision to server (local player hit asteroid). */
void dnet_send_ship_asteroid_hit(uint8_t slot, uint8_t player_id);

/** Send ADD_LOCAL_PLAYER for second controller. */
void dnet_send_add_local_player(const char* name);

/** Request server add a bot (0=easy, 1=medium, 2=hard). */
void dnet_send_add_bot(uint8_t difficulty);

/** Request server remove last bot. */
void dnet_send_remove_bot(void);

/** Remove second local player from the game. */
void dnet_send_remove_local_player(void);

/** Send input delta for second local player. */
void dnet_send_input_delta_p2(uint16_t frame_num, uint16_t input_bits);

/** Send ship state for second local player. */
void dnet_send_ship_state_p2(void);

/** Clear log lines (call when entering lobby to remove stale text). */
void dnet_clear_log(void);

/** Request leaderboard data from server. */
void dnet_request_leaderboard(void);

/**
 * Apply remote-player snapshots to the live PLAYER state.
 *
 * In RING sync mode, this walks each remote player's snapshot ring,
 * computes the bracketing-pair-interpolated pose at frame
 * (last_recv_server_frame - DNET_INTERP_LAG_FRAMES), and writes the
 * result to g_Players[pid].curPos.
 *
 * In LERP mode this is a no-op — the existing process_ship_sync handler
 * has already snapped curPos at receive time, and the per-frame remote
 * coast is handled inside applyInputBitsToPlayer (gameplay.c).
 *
 * Call once per gameplay frame in online mode, AFTER input handling
 * and BEFORE rendering.
 */
void dnet_apply_remote_snapshots(void);

/** Get the most recently received server frame number (for hit reports). */
uint16_t dnet_get_last_server_frame(void);

/** Get current sync mode (DNET_SYNC_MODE_LERP or DNET_SYNC_MODE_RING). */
uint8_t dnet_get_sync_mode(void);

#endif /* DISASTEROIDS_NET_H */
