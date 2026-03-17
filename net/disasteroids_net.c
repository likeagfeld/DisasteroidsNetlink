/**
 * disasteroids_net.c - Disasteroids Networking State Machine
 *
 * Handles the full network lifecycle following the same patterns
 * as the Coup card game's networking implementation.
 */

#include <string.h>
#include "disasteroids_net.h"

/*============================================================================
 * Static State
 *============================================================================*/

static dnet_state_data_t g_net;

/*============================================================================
 * Init / Config
 *============================================================================*/

void dnet_init(void)
{
    memset(&g_net, 0, sizeof(g_net));
    g_net.state = DNET_STATE_OFFLINE;
    g_net.modem_available = false;
    g_net.transport = (void*)0;
    g_net.status_msg = "Offline";
    dnet_rx_init(&g_net.rx, g_net.rx_buf, sizeof(g_net.rx_buf));
}

void dnet_set_modem_available(bool available)
{
    g_net.modem_available = available;
}

void dnet_set_transport(const net_transport_t* transport)
{
    g_net.transport = transport;
}

void dnet_set_username(const char* name)
{
    int i;
    for (i = 0; i < DNET_MAX_NAME && name[i]; i++)
        g_net.my_name[i] = name[i];
    g_net.my_name[i] = '\0';
}

dnet_state_t dnet_get_state(void)
{
    return g_net.state;
}

const dnet_state_data_t* dnet_get_data(void)
{
    return &g_net;
}

/*============================================================================
 * Logging
 *============================================================================*/

void dnet_log(const char* msg)
{
    int i;
    int dst;

    if (g_net.log_count < 4) {
        dst = g_net.log_count;
    } else {
        /* Shift log lines up */
        for (i = 0; i < 3; i++) {
            memcpy(g_net.log_lines[i], g_net.log_lines[i + 1], 40);
        }
        dst = 3;
    }

    for (i = 0; i < 39 && msg[i]; i++)
        g_net.log_lines[dst][i] = msg[i];
    g_net.log_lines[dst][i] = '\0';

    if (g_net.log_count < 4)
        g_net.log_count++;
}

/*============================================================================
 * State Transitions
 *============================================================================*/

void dnet_enter_offline(void)
{
    g_net.state = DNET_STATE_OFFLINE;
    g_net.status_msg = "Offline";
}

void dnet_on_connected(void)
{
    int len;

    /* Reset RX state for fresh connection */
    dnet_rx_init(&g_net.rx, g_net.rx_buf, sizeof(g_net.rx_buf));

    g_net.state = DNET_STATE_AUTHENTICATING;
    g_net.status_msg = "Authenticating...";
    g_net.auth_timer = 0;
    g_net.auth_retries = 0;
    g_net.heartbeat_counter = 0;

    /* Send CONNECT (with UUID if we have one for reconnection) */
    if (g_net.has_uuid) {
        len = dnet_encode_connect_uuid(g_net.tx_buf, g_net.my_uuid);
    } else {
        len = dnet_encode_connect(g_net.tx_buf);
    }
    net_transport_send(g_net.transport, g_net.tx_buf, len);

    dnet_log("Sent CONNECT");
}

/*============================================================================
 * Message Processing
 *============================================================================*/

static void process_welcome(const uint8_t* payload, int len)
{
    int off;
    int i;

    if (len < 2) return;

    /* [uid:1][uuid:36][name_len:1][name:N] */
    g_net.my_player_id = payload[1];
    off = 2;

    /* Read UUID */
    if (off + SNCP_UUID_LEN <= len) {
        for (i = 0; i < SNCP_UUID_LEN; i++)
            g_net.my_uuid[i] = (char)payload[off + i];
        g_net.my_uuid[SNCP_UUID_LEN] = '\0';
        g_net.has_uuid = true;
        off += SNCP_UUID_LEN;
    }

    /* Read back username if provided */
    if (off < len) {
        dnet_read_string(&payload[off], len - off,
                         g_net.my_name, DNET_MAX_NAME + 1);
    }

    g_net.state = DNET_STATE_LOBBY;
    g_net.status_msg = "In Lobby";
    dnet_log("Welcome! You are Player");
}

static void process_username_required(void)
{
    int len;

    g_net.state = DNET_STATE_USERNAME;
    g_net.status_msg = "Enter username";

    /* If we already have a name set, send it immediately */
    if (g_net.my_name[0] != '\0') {
        len = dnet_encode_set_username(g_net.tx_buf, g_net.my_name);
        net_transport_send(g_net.transport, g_net.tx_buf, len);
        g_net.state = DNET_STATE_AUTHENTICATING;
        g_net.status_msg = "Authenticating...";
    }
}

static void process_lobby_state(const uint8_t* payload, int len)
{
    int off, i, consumed;

    if (len < 2) return;

    g_net.lobby_count = payload[1];
    if (g_net.lobby_count > DNET_MAX_PLAYERS)
        g_net.lobby_count = DNET_MAX_PLAYERS;

    off = 2;
    for (i = 0; i < g_net.lobby_count && off < len; i++) {
        if (off >= len) break;
        g_net.lobby_players[i].id = payload[off++];

        consumed = dnet_read_string(&payload[off], len - off,
                                     g_net.lobby_players[i].name,
                                     DNET_MAX_NAME + 1);
        if (consumed < 0) break;
        off += consumed;

        if (off < len)
            g_net.lobby_players[i].ready = (payload[off++] != 0);

        g_net.lobby_players[i].active = true;
    }

    /* Clear remaining slots */
    for (; i < DNET_MAX_PLAYERS; i++) {
        g_net.lobby_players[i].active = false;
    }
}

static void process_game_start(const uint8_t* payload, int len)
{
    if (len < 8) return;

    /* [seed:4 BE][my_player_id:1][opponent_count:1][game_type:1][num_lives:1] */
    g_net.game_seed = ((uint32_t)payload[1] << 24)
                    | ((uint32_t)payload[2] << 16)
                    | ((uint32_t)payload[3] << 8)
                    | ((uint32_t)payload[4]);
    g_net.my_player_id = payload[5];
    g_net.opponent_count = payload[6];
    g_net.game_type = payload[7];
    if (len > 8) g_net.num_lives = payload[8];

    g_net.state = DNET_STATE_PLAYING;
    g_net.status_msg = "Playing";
    g_net.local_frame = 0;
    g_net.last_sent_input = 0;
    g_net.send_cooldown = 15; /* Force immediate send on first frame */

    /* Clear per-player input buffers */
    memset(g_net.remote_inputs, 0, sizeof(g_net.remote_inputs));
    memset(g_net.remote_input_head, 0, sizeof(g_net.remote_input_head));

    dnet_log("Game starting!");
}

static void process_input_relay(const uint8_t* payload, int len)
{
    uint8_t player_id;
    uint16_t frame_num;
    uint16_t input_bits;
    int idx;

    if (len < 6) return;

    /* [player_id:1][frame:2 BE][input:2 BE] */
    player_id = payload[1];
    frame_num = ((uint16_t)payload[2] << 8) | (uint16_t)payload[3];
    input_bits = ((uint16_t)payload[4] << 8) | (uint16_t)payload[5];

    /* Don't store our own input (server echoes to all) */
    if (player_id == g_net.my_player_id) return;
    if (player_id >= DNET_MAX_PLAYERS) return;

    /* Store in per-player ring buffer */
    idx = g_net.remote_input_head[player_id] % DNET_INPUT_BUFFER_PER_PLAYER;
    g_net.remote_inputs[player_id][idx].frame_num = frame_num;
    g_net.remote_inputs[player_id][idx].input_bits = input_bits;
    g_net.remote_inputs[player_id][idx].player_id = player_id;
    g_net.remote_inputs[player_id][idx].valid = true;
    g_net.remote_input_head[player_id]++;
}

static void process_game_over(const uint8_t* payload, int len)
{
    (void)payload;
    (void)len;
    dnet_log("Game Over!");
}

static void process_pause_ack(const uint8_t* payload, int len)
{
    (void)payload;
    (void)len;
    /* Game code handles actual pause state change */
}

static void process_log(const uint8_t* payload, int len)
{
    char msg[40];
    if (len < 2) return;
    dnet_read_string(&payload[1], len - 1, msg, sizeof(msg));
    dnet_log(msg);
}

static void process_message(const uint8_t* payload, int len)
{
    uint8_t msg_type;

    if (len < 1) return;
    msg_type = payload[0];

    switch (msg_type) {
    case SNCP_MSG_WELCOME:
    case SNCP_MSG_WELCOME_BACK:
        process_welcome(payload, len);
        break;

    case SNCP_MSG_USERNAME_REQUIRED:
        process_username_required();
        break;

    case SNCP_MSG_USERNAME_TAKEN:
    {
        /* Auto-retry with a numeric suffix: PLAYER -> PLAYER1 -> PLAYER2 ... */
        if (g_net.username_retry < 9) {
            int nlen = 0;
            int slen;
            g_net.username_retry++;
            while (g_net.my_name[nlen] && nlen < DNET_MAX_NAME) nlen++;
            /* Strip previous digit suffix if any */
            if (nlen > 0 && g_net.my_name[nlen - 1] >= '1' &&
                g_net.my_name[nlen - 1] <= '9') {
                nlen--;
            }
            if (nlen < DNET_MAX_NAME) {
                g_net.my_name[nlen] = '0' + g_net.username_retry;
                g_net.my_name[nlen + 1] = '\0';
            }
            slen = dnet_encode_set_username(g_net.tx_buf, g_net.my_name);
            net_transport_send(g_net.transport, g_net.tx_buf, slen);
            g_net.state = DNET_STATE_AUTHENTICATING;
            dnet_log("Name taken, trying...");
        } else {
            dnet_log("All names taken!");
            g_net.state = DNET_STATE_DISCONNECTED;
            g_net.status_msg = "Name unavailable";
        }
        break;
    }

    case DNET_MSG_LOBBY_STATE:
        process_lobby_state(payload, len);
        break;

    case DNET_MSG_GAME_START:
        process_game_start(payload, len);
        break;

    case DNET_MSG_INPUT_RELAY:
        process_input_relay(payload, len);
        break;

    case DNET_MSG_GAME_OVER:
        process_game_over(payload, len);
        break;

    case DNET_MSG_PAUSE_ACK:
        process_pause_ack(payload, len);
        break;

    case DNET_MSG_LOG:
        process_log(payload, len);
        break;

    case DNET_MSG_PLAYER_JOIN:
        dnet_log("Player joined!");
        break;

    case DNET_MSG_PLAYER_LEAVE:
        dnet_log("Player left!");
        break;

    default:
        break;
    }
}

/*============================================================================
 * Network Tick (call every frame)
 *============================================================================*/

void dnet_tick(void)
{
    int processed;
    int len;

    g_net.frame_count++;

    if (g_net.state == DNET_STATE_OFFLINE ||
        g_net.state == DNET_STATE_DISCONNECTED) {
        return;
    }

    if (!g_net.transport) return;

    /* Process incoming messages (bounded per frame) */
    processed = 0;
    while (processed < DNET_MAX_PACKETS_FRAME) {
        len = dnet_rx_poll(&g_net.rx, g_net.transport);
        if (len <= 0) break;
        process_message(g_net.rx_buf, len);
        processed++;
    }

    /* Auth retry logic */
    if (g_net.state == DNET_STATE_AUTHENTICATING) {
        g_net.auth_timer++;
        if (g_net.auth_timer >= DNET_AUTH_TIMEOUT) {
            g_net.auth_timer = 0;
            g_net.auth_retries++;
            if (g_net.auth_retries >= DNET_AUTH_MAX_RETRIES) {
                dnet_log("Auth failed - timeout");
                g_net.state = DNET_STATE_DISCONNECTED;
                g_net.status_msg = "Auth timeout";
                return;
            }
            /* Retry CONNECT */
            if (g_net.has_uuid) {
                len = dnet_encode_connect_uuid(g_net.tx_buf, g_net.my_uuid);
            } else {
                len = dnet_encode_connect(g_net.tx_buf);
            }
            net_transport_send(g_net.transport, g_net.tx_buf, len);
            dnet_log("Retrying auth...");
        }
    }

    /* Heartbeat */
    g_net.heartbeat_counter++;
    if (g_net.heartbeat_counter >= DNET_HEARTBEAT_INTERVAL) {
        g_net.heartbeat_counter = 0;
        len = dnet_encode_heartbeat(g_net.tx_buf);
        net_transport_send(g_net.transport, g_net.tx_buf, len);
    }
}

/*============================================================================
 * Send Functions
 *============================================================================*/

void dnet_send_input(uint16_t frame_num, uint16_t input_bits)
{
    int len;
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;
    len = dnet_encode_input_state(g_net.tx_buf, frame_num, input_bits);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
    g_net.local_frame = frame_num;
}

void dnet_send_input_delta(uint16_t frame_num, uint16_t input_bits)
{
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;

    if (input_bits != g_net.last_sent_input || g_net.send_cooldown >= 15) {
        int len = dnet_encode_input_state(g_net.tx_buf, frame_num, input_bits);
        net_transport_send(g_net.transport, g_net.tx_buf, len);
        g_net.last_sent_input = input_bits;
        g_net.send_cooldown = 0;
    } else {
        g_net.send_cooldown++;
    }

    g_net.local_frame = frame_num;
}

void dnet_send_ready(void)
{
    int len;
    if (g_net.state != DNET_STATE_LOBBY || !g_net.transport) return;
    g_net.my_ready = !g_net.my_ready;
    len = dnet_encode_ready(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_start_game(void)
{
    int len;
    if (g_net.state != DNET_STATE_LOBBY || !g_net.transport) return;
    len = dnet_encode_start_game(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_disconnect(void)
{
    int len;
    if (!g_net.transport) return;
    if (g_net.state == DNET_STATE_OFFLINE) return;
    len = dnet_encode_disconnect(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
    g_net.state = DNET_STATE_DISCONNECTED;
    g_net.status_msg = "Disconnected";
}

void dnet_send_pause(void)
{
    int len;
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;
    len = dnet_encode_pause(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

int dnet_get_remote_input(uint16_t frame_num, uint8_t player_id)
{
    int i;
    int best = -1;
    uint16_t best_frame = 0;

    if (player_id >= DNET_MAX_PLAYERS) return -1;

    /* Search this player's ring buffer; prefer exact frame match,
     * fall back to the most recent input. */
    for (i = 0; i < DNET_INPUT_BUFFER_PER_PLAYER; i++) {
        if (!g_net.remote_inputs[player_id][i].valid)
            continue;

        /* Exact frame match — return immediately */
        if (g_net.remote_inputs[player_id][i].frame_num == frame_num) {
            return (int)g_net.remote_inputs[player_id][i].input_bits;
        }

        /* Track the most recent input from this player */
        if (best < 0 || g_net.remote_inputs[player_id][i].frame_num > best_frame) {
            best_frame = g_net.remote_inputs[player_id][i].frame_num;
            best = (int)g_net.remote_inputs[player_id][i].input_bits;
        }
    }

    return best; /* Most recent input, or -1 if none */
}
