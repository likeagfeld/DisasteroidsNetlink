/**
 * disasteroids_protocol.h - Disasteroids Network Protocol
 *
 * Binary protocol using SNCP framing for Disasteroids multiplayer.
 * Uses the same [LEN_HI][LEN_LO][PAYLOAD...] framing as the Coup protocol.
 * Reuses SNCP auth handshake (CONNECT/WELCOME) for player authentication.
 *
 * Networking model: SERVER-AUTHORITATIVE STATE SYNC
 *   Each Saturn sends its local player inputs and ship state.
 *   The server owns all randomness, asteroid spawning, and collision authority.
 *   Saturns run deterministic asteroid physics from server-provided initial data.
 *   Server assigns player IDs and manages lobby/game lifecycle.
 *
 * Header-only: all functions are static inline.
 */

#ifndef DISASTEROIDS_PROTOCOL_H
#define DISASTEROIDS_PROTOCOL_H

#include <stdint.h>
#include <string.h>
#include "net_transport.h"

/*============================================================================
 * SNCP Auth Messages (shared with Coup)
 *============================================================================*/

#define SNCP_MSG_CONNECT           0x01
#define SNCP_MSG_SET_USERNAME      0x02
#define SNCP_MSG_HEARTBEAT         0x04
#define SNCP_MSG_DISCONNECT        0x05

#define SNCP_MSG_USERNAME_REQUIRED 0x81
#define SNCP_MSG_WELCOME           0x82
#define SNCP_MSG_WELCOME_BACK      0x83
#define SNCP_MSG_USERNAME_TAKEN    0x84

#define SNCP_UUID_LEN              36

/*============================================================================
 * Disasteroids Client -> Server Messages (0x10 - 0x1F)
 *============================================================================*/

#define DNET_MSG_READY             0x10  /* Toggle ready state (no payload) */
#define DNET_MSG_INPUT_STATE       0x11  /* Per-frame input [frame:2 BE][input:2 BE] */
#define DNET_MSG_START_GAME_REQ    0x12  /* Request game start (no payload) */
#define DNET_MSG_PAUSE_REQ         0x13  /* Request pause toggle (no payload) */
#define DNET_MSG_SHIP_STATE        0x14  /* [x:4][y:4][dx:4][dy:4][rot:2][flags:1] */
#define DNET_MSG_ASTEROID_HIT      0x15  /* [slot:1][scorer_player_id:1] */
#define DNET_MSG_ADD_LOCAL_PLAYER  0x16  /* [name_len:1][name:N] */
#define DNET_MSG_ADD_BOT           0x17  /* [difficulty:1] (0=easy,1=medium,2=hard) */
#define DNET_MSG_REMOVE_BOT        0x18  /* (no payload) */
#define DNET_MSG_REMOVE_LOCAL_PLAYER 0x19 /* (no payload) - remove second local player */
#define DNET_MSG_SHIP_ASTEROID_HIT 0x1A  /* [slot:1][player_id:1] local player hit asteroid */

/*============================================================================
 * Disasteroids Server -> Client Messages (0xA0 - 0xBF)
 *============================================================================*/

#define DNET_MSG_LOBBY_STATE       0xA0  /* [count:1][{id:1,name:LP,ready:1}...] */
#define DNET_MSG_GAME_START        0xA1  /* [seed:4 BE][my_player_id:1][opponent_count:1][game_type:1][num_lives:1] */
#define DNET_MSG_INPUT_RELAY       0xA2  /* [player_id:1][frame:2 BE][input:2 BE] */
#define DNET_MSG_PLAYER_JOIN       0xA3  /* [id:1][name:LP] */
#define DNET_MSG_PLAYER_LEAVE      0xA4  /* [id:1] */
#define DNET_MSG_GAME_OVER         0xA5  /* [winner_id:1] */
#define DNET_MSG_LOG               0xA6  /* [len:1][text:N] */
#define DNET_MSG_PAUSE_ACK         0xA7  /* [paused:1] */
#define DNET_MSG_SETTINGS_UPDATE   0xA8  /* [game_type:1][num_lives:1] */
#define DNET_MSG_SHIP_SYNC         0xA9  /* [player_id:1][x:4][y:4][dx:4][dy:4][rot:2][flags:1] */
#define DNET_MSG_ASTEROID_SPAWN    0xAA  /* [slot:1][x:4][y:4][dx:4][dy:4][size:1][type:1] */
#define DNET_MSG_ASTEROID_DESTROY  0xAB  /* [slot:1][scorer_id:1][num_children:1]{children} */
#define DNET_MSG_WAVE_EVENT        0xAC  /* [wave:1][count:1][timer:2]{asteroids} */
#define DNET_MSG_PLAYER_KILL       0xAD  /* [player_id:1][lives:1][angle:2][invuln:2][respawn:2] */
#define DNET_MSG_PLAYER_SPAWN      0xAE  /* [player_id:1][angle:2][invuln:2] */
#define DNET_MSG_LOCAL_PLAYER_ACK  0x86  /* [player_id:1] */

/*============================================================================
 * Input State Bitmask (matches Jo Engine key definitions)
 *
 * We pack the relevant gameplay inputs into a 16-bit bitmask.
 * This is what gets sent every frame.
 *============================================================================*/

#define DNET_INPUT_UP      (1 << 0)
#define DNET_INPUT_DOWN    (1 << 1)
#define DNET_INPUT_LEFT    (1 << 2)
#define DNET_INPUT_RIGHT   (1 << 3)
#define DNET_INPUT_A       (1 << 4)   /* Shoot */
#define DNET_INPUT_B       (1 << 5)   /* Thrust */
#define DNET_INPUT_C       (1 << 6)   /* Shoot */
#define DNET_INPUT_X       (1 << 7)   /* Change color */
#define DNET_INPUT_START   (1 << 8)   /* Pause */

/*============================================================================
 * Buffer Sizes
 *============================================================================*/

#define DNET_RX_FRAME_SIZE  512
#define DNET_TX_FRAME_SIZE  64

/*============================================================================
 * Frame Send/Receive (SNCP framing)
 *============================================================================*/

/**
 * Send a binary frame: [LEN_HI][LEN_LO][payload...]
 */
static inline void dnet_send_frame(const net_transport_t* transport,
                                    const uint8_t* payload, int payload_len)
{
    uint8_t hdr[2];
    hdr[0] = (uint8_t)((payload_len >> 8) & 0xFF);
    hdr[1] = (uint8_t)(payload_len & 0xFF);
    net_transport_send(transport, hdr, 2);
    net_transport_send(transport, payload, payload_len);
}

/**
 * Receive state machine (identical to SNCP/Coup).
 */
typedef struct {
    uint8_t* buf;
    int      buf_size;
    int      rx_pos;
    int      frame_len;
} dnet_rx_state_t;

static inline void dnet_rx_init(dnet_rx_state_t* st, uint8_t* buf, int buf_size)
{
    st->buf = buf;
    st->buf_size = buf_size;
    st->rx_pos = 0;
    st->frame_len = -1;
}

/* Max UART bytes to process per poll call.  Bounds worst-case A-bus
 * stall time so rendering isn't starved.  At 14,400 baud the modem
 * delivers ~24 bytes/frame at 60fps; leftover bytes stay in the
 * UART FIFO and are drained on the next frame.  48 is safe even at
 * 28,800 baud (Coup uses this value). */
#define DNET_RX_MAX_PER_POLL  48

/**
 * Poll for a complete frame. Returns:
 *   >0 = frame length (payload in st->buf[0..len-1])
 *    0 = incomplete (call again next frame)
 *   -1 = error (frame too large or zero-length)
 */
static inline int dnet_rx_poll(dnet_rx_state_t* st,
                                const net_transport_t* transport)
{
    int bytes_read = 0;
    while (bytes_read < DNET_RX_MAX_PER_POLL && net_transport_rx_ready(transport)) {
        uint8_t b = net_transport_rx_byte(transport);
        bytes_read++;

        if (st->frame_len < 0) {
            st->buf[st->rx_pos++] = b;
            if (st->rx_pos == 2) {
                st->frame_len = ((int)st->buf[0] << 8) | (int)st->buf[1];
                st->rx_pos = 0;
                if (st->frame_len > st->buf_size || st->frame_len == 0) {
                    st->frame_len = -1;
                    st->rx_pos = 0;
                    return -1;
                }
            }
        } else {
            st->buf[st->rx_pos++] = b;
            if (st->rx_pos >= st->frame_len) {
                int len = st->frame_len;
                st->frame_len = -1;
                st->rx_pos = 0;
                return len;
            }
        }
    }
    return 0;
}

/*============================================================================
 * Decode Helpers
 *============================================================================*/

static inline int dnet_read_string(const uint8_t* p, int remaining,
                                    char* dst, int max)
{
    int slen, copy, i;
    if (remaining < 1) { dst[0] = '\0'; return -1; }
    slen = (int)p[0];
    if (remaining < 1 + slen) { dst[0] = '\0'; return -1; }
    copy = (slen < max - 1) ? slen : (max - 1);
    for (i = 0; i < copy; i++) dst[i] = (char)p[1 + i];
    dst[copy] = '\0';
    return 1 + slen;
}

/*============================================================================
 * Client -> Server Encode Functions
 * All return total frame size (header + payload).
 *============================================================================*/

/** Encode CONNECT (new user, no UUID). */
static inline int dnet_encode_connect(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = SNCP_MSG_CONNECT;
    return 3;
}

/** Encode CONNECT with UUID for reconnection. */
static inline int dnet_encode_connect_uuid(uint8_t* buf, const char* uuid)
{
    int i;
    buf[0] = 0x00;
    buf[1] = 37;
    buf[2] = SNCP_MSG_CONNECT;
    for (i = 0; i < SNCP_UUID_LEN; i++)
        buf[3 + i] = (uint8_t)uuid[i];
    return 3 + SNCP_UUID_LEN;
}

/** Encode SET_USERNAME. */
static inline int dnet_encode_set_username(uint8_t* buf, const char* name)
{
    int nlen = 0;
    int payload_len;
    int i;
    while (name[nlen]) nlen++;
    if (nlen > 16) nlen = 16;
    payload_len = 1 + 1 + nlen;
    buf[0] = (uint8_t)((payload_len >> 8) & 0xFF);
    buf[1] = (uint8_t)(payload_len & 0xFF);
    buf[2] = SNCP_MSG_SET_USERNAME;
    buf[3] = (uint8_t)nlen;
    for (i = 0; i < nlen; i++)
        buf[4 + i] = (uint8_t)name[i];
    return 2 + payload_len;
}

/** Encode DISCONNECT. */
static inline int dnet_encode_disconnect(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = SNCP_MSG_DISCONNECT;
    return 3;
}

/** Encode HEARTBEAT. */
static inline int dnet_encode_heartbeat(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = SNCP_MSG_HEARTBEAT;
    return 3;
}

/** Encode READY toggle. */
static inline int dnet_encode_ready(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = DNET_MSG_READY;
    return 3;
}

/** Encode START_GAME request. */
static inline int dnet_encode_start_game(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = DNET_MSG_START_GAME_REQ;
    return 3;
}

/**
 * Encode INPUT_STATE: per-frame input for the local player.
 * [frame_hi:1][frame_lo:1][input_hi:1][input_lo:1]
 */
static inline int dnet_encode_input_state(uint8_t* buf,
                                           uint16_t frame_num,
                                           uint16_t input_bits)
{
    buf[0] = 0x00;
    buf[1] = 0x05;  /* payload = type(1) + frame(2) + input(2) */
    buf[2] = DNET_MSG_INPUT_STATE;
    buf[3] = (uint8_t)((frame_num >> 8) & 0xFF);
    buf[4] = (uint8_t)(frame_num & 0xFF);
    buf[5] = (uint8_t)((input_bits >> 8) & 0xFF);
    buf[6] = (uint8_t)(input_bits & 0xFF);
    return 7;
}

/** Encode PAUSE request. */
static inline int dnet_encode_pause(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = DNET_MSG_PAUSE_REQ;
    return 3;
}

/**
 * Encode SHIP_STATE: local ship position for server collision + relay.
 * [x:4][y:4][dx:4][dy:4][rot:2][flags:1] = 19 bytes payload, 21 total.
 * x/y/dx/dy are jo_fixed (32-bit). rot is int16. flags: b0=alive, b1=invuln, b2=thrust.
 */
static inline int dnet_encode_ship_state(uint8_t* buf,
                                          int32_t x, int32_t y,
                                          int32_t dx, int32_t dy,
                                          int16_t rot, uint8_t flags)
{
    /* payload = type(1) + x(4) + y(4) + dx(4) + dy(4) + rot(2) + flags(1) = 20 */
    buf[0] = 0x00;
    buf[1] = 20;
    buf[2] = DNET_MSG_SHIP_STATE;
    buf[3]  = (uint8_t)((x >> 24) & 0xFF);
    buf[4]  = (uint8_t)((x >> 16) & 0xFF);
    buf[5]  = (uint8_t)((x >> 8) & 0xFF);
    buf[6]  = (uint8_t)(x & 0xFF);
    buf[7]  = (uint8_t)((y >> 24) & 0xFF);
    buf[8]  = (uint8_t)((y >> 16) & 0xFF);
    buf[9]  = (uint8_t)((y >> 8) & 0xFF);
    buf[10] = (uint8_t)(y & 0xFF);
    buf[11] = (uint8_t)((dx >> 24) & 0xFF);
    buf[12] = (uint8_t)((dx >> 16) & 0xFF);
    buf[13] = (uint8_t)((dx >> 8) & 0xFF);
    buf[14] = (uint8_t)(dx & 0xFF);
    buf[15] = (uint8_t)((dy >> 24) & 0xFF);
    buf[16] = (uint8_t)((dy >> 16) & 0xFF);
    buf[17] = (uint8_t)((dy >> 8) & 0xFF);
    buf[18] = (uint8_t)(dy & 0xFF);
    buf[19] = (uint8_t)((rot >> 8) & 0xFF);
    buf[20] = (uint8_t)(rot & 0xFF);
    buf[21] = flags;
    return 22;  /* 2 header + 20 payload */
}

/**
 * Encode ASTEROID_HIT: projectile-asteroid collision detected locally.
 * [slot:1][scorer_player_id:1] = 2 bytes payload, 4 total.
 */
static inline int dnet_encode_asteroid_hit(uint8_t* buf,
                                            uint8_t slot,
                                            uint8_t scorer_id)
{
    buf[0] = 0x00;
    buf[1] = 3;   /* payload = type(1) + slot(1) + scorer(1) */
    buf[2] = DNET_MSG_ASTEROID_HIT;
    buf[3] = slot;
    buf[4] = scorer_id;
    return 5;  /* 2 header + 3 payload */
}

/**
 * Encode SHIP_ASTEROID_HIT: local player collided with asteroid.
 * [slot:1][player_id:1] = 2 bytes payload, 5 total (with SNCP header).
 */
static inline int dnet_encode_ship_asteroid_hit(uint8_t* buf,
                                                 uint8_t slot,
                                                 uint8_t player_id)
{
    buf[0] = 0x00;
    buf[1] = 3;   /* payload = type(1) + slot(1) + player_id(1) */
    buf[2] = DNET_MSG_SHIP_ASTEROID_HIT;
    buf[3] = slot;
    buf[4] = player_id;
    return 5;  /* 2 header + 3 payload */
}

/** Encode ADD_BOT: request server add a bot with given difficulty. */
static inline int dnet_encode_add_bot(uint8_t* buf, uint8_t difficulty)
{
    buf[0] = 0x00;
    buf[1] = 0x02;  /* payload = type(1) + difficulty(1) */
    buf[2] = DNET_MSG_ADD_BOT;
    buf[3] = difficulty;
    return 4;
}

/** Encode REMOVE_BOT: request server remove the last bot. */
static inline int dnet_encode_remove_bot(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = DNET_MSG_REMOVE_BOT;
    return 3;
}

/**
 * Encode ADD_LOCAL_PLAYER: register a second local player on this connection.
 * [name_len:1][name:N]
 */
static inline int dnet_encode_add_local_player(uint8_t* buf, const char* name)
{
    int nlen = 0;
    int payload_len;
    int i;
    while (name[nlen]) nlen++;
    if (nlen > 16) nlen = 16;
    payload_len = 1 + 1 + nlen;  /* type + name_len + name */
    buf[0] = (uint8_t)((payload_len >> 8) & 0xFF);
    buf[1] = (uint8_t)(payload_len & 0xFF);
    buf[2] = DNET_MSG_ADD_LOCAL_PLAYER;
    buf[3] = (uint8_t)nlen;
    for (i = 0; i < nlen; i++)
        buf[4 + i] = (uint8_t)name[i];
    return 2 + payload_len;
}

/** Encode REMOVE_LOCAL_PLAYER: remove the second local player. */
static inline int dnet_encode_remove_local_player(uint8_t* buf)
{
    buf[0] = 0x00;
    buf[1] = 0x01;
    buf[2] = DNET_MSG_REMOVE_LOCAL_PLAYER;
    return 3;
}

/**
 * Encode INPUT_STATE with explicit player_id (for second local player).
 * [type:1][player_id:1][frame:2 BE][input:2 BE] = 6 bytes payload
 */
static inline int dnet_encode_input_state_p2(uint8_t* buf,
                                              uint8_t player_id,
                                              uint16_t frame_num,
                                              uint16_t input_bits)
{
    buf[0] = 0x00;
    buf[1] = 0x06;  /* payload = type(1) + pid(1) + frame(2) + input(2) */
    buf[2] = DNET_MSG_INPUT_STATE;
    buf[3] = player_id;
    buf[4] = (uint8_t)((frame_num >> 8) & 0xFF);
    buf[5] = (uint8_t)(frame_num & 0xFF);
    buf[6] = (uint8_t)((input_bits >> 8) & 0xFF);
    buf[7] = (uint8_t)(input_bits & 0xFF);
    return 8;
}

/**
 * Encode SHIP_STATE with explicit player_id (for second local player).
 * [type:1][player_id:1][x:4][y:4][dx:4][dy:4][rot:2][flags:1] = 21 bytes payload
 */
static inline int dnet_encode_ship_state_p2(uint8_t* buf,
                                             uint8_t player_id,
                                             int32_t x, int32_t y,
                                             int32_t dx, int32_t dy,
                                             int16_t rot, uint8_t flags)
{
    buf[0] = 0x00;
    buf[1] = 21;
    buf[2] = DNET_MSG_SHIP_STATE;
    buf[3] = player_id;
    buf[4]  = (uint8_t)((x >> 24) & 0xFF);
    buf[5]  = (uint8_t)((x >> 16) & 0xFF);
    buf[6]  = (uint8_t)((x >> 8) & 0xFF);
    buf[7]  = (uint8_t)(x & 0xFF);
    buf[8]  = (uint8_t)((y >> 24) & 0xFF);
    buf[9]  = (uint8_t)((y >> 16) & 0xFF);
    buf[10] = (uint8_t)((y >> 8) & 0xFF);
    buf[11] = (uint8_t)(y & 0xFF);
    buf[12] = (uint8_t)((dx >> 24) & 0xFF);
    buf[13] = (uint8_t)((dx >> 16) & 0xFF);
    buf[14] = (uint8_t)((dx >> 8) & 0xFF);
    buf[15] = (uint8_t)(dx & 0xFF);
    buf[16] = (uint8_t)((dy >> 24) & 0xFF);
    buf[17] = (uint8_t)((dy >> 16) & 0xFF);
    buf[18] = (uint8_t)((dy >> 8) & 0xFF);
    buf[19] = (uint8_t)(dy & 0xFF);
    buf[20] = (uint8_t)((rot >> 8) & 0xFF);
    buf[21] = (uint8_t)(rot & 0xFF);
    buf[22] = flags;
    return 23;  /* 2 header + 21 payload */
}

#endif /* DISASTEROIDS_PROTOCOL_H */
