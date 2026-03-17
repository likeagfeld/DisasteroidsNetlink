/**
 * disasteroids_net.c - Disasteroids Networking State Machine
 *
 * Handles the full network lifecycle following the same patterns
 * as the Coup card game's networking implementation.
 */

#include <string.h>
#include "disasteroids_net.h"
#include "../main.h"
#include "../objects/disasteroid.h"
#include "../objects/ship.h"
#include "../objects/explosion.h"
#include "../objects/projectile.h"
#include "../assets/assets.h"

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

    /* If we have a second local player, register them now */
    if (g_Game.hasSecondLocal && g_Game.playerName2[0] != '\0') {
        int slen = dnet_encode_add_local_player(g_net.tx_buf, g_Game.playerName2);
        net_transport_send(g_net.transport, g_net.tx_buf, slen);
        dnet_log("Registering Player 2...");
    }
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
    g_net.num_lives = 3; /* default */
    if (len > 8) g_net.num_lives = payload[8];

    /* Validate player ID is within bounds */
    if (g_net.my_player_id >= MAX_PLAYERS) {
        dnet_log("Bad player ID from server!");
        g_net.state = DNET_STATE_DISCONNECTED;
        g_net.status_msg = "Server error";
        return;
    }

    g_net.state = DNET_STATE_PLAYING;
    g_net.status_msg = "Playing";
    g_net.local_frame = 0;
    g_net.last_sent_input = 0;
    g_net.send_cooldown = 15; /* Force immediate send on first frame */
    g_net.ship_state_cooldown = 10; /* Force immediate ship state send */
    g_net.last_sent_input_p2 = 0;
    g_net.send_cooldown_p2 = 15;
    g_net.ship_state_cooldown_p2 = 10;
    g_net.server_auth_mode = true;

    /* Clear per-player input buffers */
    memset(g_net.remote_inputs, 0, sizeof(g_net.remote_inputs));
    memset(g_net.remote_input_head, 0, sizeof(g_net.remote_input_head));

    /* Clear lobby roster — server will resend via PLAYER_JOIN with game IDs */
    memset(g_net.lobby_players, 0, sizeof(g_net.lobby_players));
    g_net.lobby_count = 0;

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
    if (g_Game.hasSecondLocal && player_id == g_Game.myPlayerID2) return;
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

    /* Trigger game over state — countdown then return to lobby */
    g_Game.isGameOver = true;
    g_Game.gameOverFrames = GAME_OVER_TIMER;

    /* Return network state to lobby so we can rejoin */
    g_net.state = DNET_STATE_LOBBY;
    g_net.status_msg = "In Lobby";
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

/*============================================================================
 * Byte-read helpers (big-endian)
 *============================================================================*/

static inline int32_t read_i32(const uint8_t* p)
{
    return (int32_t)(((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
                   | ((uint32_t)p[2] << 8) | (uint32_t)p[3]);
}

static inline int16_t read_i16(const uint8_t* p)
{
    return (int16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static inline uint16_t read_u16(const uint8_t* p)
{
    return ((uint16_t)p[0] << 8) | (uint16_t)p[1];
}

/*============================================================================
 * Server-Authoritative Message Handlers
 *============================================================================*/

extern DISASTEROID g_Disasteroids[MAX_DISASTEROIDS];

static void process_ship_sync(const uint8_t* payload, int len)
{
    uint8_t pid;
    PPLAYER player;

    /* [type:1][player_id:1][x:4][y:4][dx:4][dy:4][rot:2][flags:1] = 21 */
    if (len < 21) return;

    pid = payload[1];
    if (pid >= MAX_PLAYERS) return;
    if (pid == g_net.my_player_id) return; /* ignore own echo */
    if (g_Game.hasSecondLocal && pid == g_Game.myPlayerID2) return; /* ignore P2 echo */

    player = &g_Players[pid];
    if (player->objectState != OBJECT_STATE_ACTIVE) return;

    /* Snap velocity and rotation from server */
    player->curPos.dx = (jo_fixed)read_i32(&payload[10]);
    player->curPos.dy = (jo_fixed)read_i32(&payload[14]);
    player->curPos.rot = (jo_fixed)read_i16(&payload[18]);

    /* flags: bit0=alive, bit1=invuln, bit2=thrust */
    player->isThrusting = (payload[20] & 0x04) ? true : false;

    /* Smooth position correction toward server position (wrap-aware) */
    {
        jo_fixed tx = (jo_fixed)read_i32(&payload[2]);
        jo_fixed ty = (jo_fixed)read_i32(&payload[6]);
        jo_fixed dx_pos = tx - player->curPos.x;
        jo_fixed dy_pos = ty - player->curPos.y;

        /* If delta is larger than half the screen, wrap the short way */
        if (dx_pos > SCREEN_MAX_X)       dx_pos -= (SCREEN_MAX_X - SCREEN_MIN_X);
        else if (dx_pos < SCREEN_MIN_X)  dx_pos += (SCREEN_MAX_X - SCREEN_MIN_X);
        if (dy_pos > SCREEN_MAX_Y)       dy_pos -= (SCREEN_MAX_Y - SCREEN_MIN_Y);
        else if (dy_pos < SCREEN_MIN_Y)  dy_pos += (SCREEN_MAX_Y - SCREEN_MIN_Y);

        /* Small error (<3 pixels): snap directly. Large error: lerp 75%. */
        if (JO_ABS(dx_pos) < toFIXED(3) && JO_ABS(dy_pos) < toFIXED(3)) {
            player->curPos.x = tx;
            player->curPos.y = ty;
        } else {
            /* 75% correction: move 3/4 of the way to server position */
            player->curPos.x += dx_pos - (dx_pos >> 2);
            player->curPos.y += dy_pos - (dy_pos >> 2);
        }
    }
}

static void process_asteroid_spawn(const uint8_t* payload, int len)
{
    /* [type:1][slot:1][x:4][y:4][dx:4][dy:4][size:1][type:1] = 20 */
    if (len < 20) return;

    spawnDisasteroidFromServer(
        payload[1],
        (jo_fixed)read_i32(&payload[2]),
        (jo_fixed)read_i32(&payload[6]),
        (jo_fixed)read_i32(&payload[10]),
        (jo_fixed)read_i32(&payload[14]),
        payload[18],
        payload[19]
    );
}

static void process_asteroid_destroy(const uint8_t* payload, int len)
{
    uint8_t slot, scorer_id, num_children;
    int off, i;

    /* [type:1][slot:1][scorer_id:1][num_children:1] = 4 minimum */
    if (len < 4) return;

    slot = payload[1];
    scorer_id = payload[2];
    num_children = payload[3];

    /* Award score if scorer is valid */
    if (scorer_id != 0xFF && scorer_id < MAX_PLAYERS)
    {
        g_Players[scorer_id].score.points += DISASTEROID_DESTROY_POINTS;
    }

    /* Spawn children from server data before destroying parent */
    off = 4;
    for (i = 0; i < num_children && off + 11 <= len; i++)
    {
        /* [child_slot:1][child_dx:4][child_dy:4][child_size:1][child_type:1] = 11 */
        splitDisasteroidFromServer(
            slot,
            payload[off],
            (jo_fixed)read_i32(&payload[off + 1]),
            (jo_fixed)read_i32(&payload[off + 5]),
            payload[off + 9],
            payload[off + 10]
        );
        off += 11;
    }

    /* Destroy the parent asteroid (cosmetic effects) */
    destroyDisasteroidFromServer(slot);
}

static void process_wave_event(const uint8_t* payload, int len)
{
    uint8_t wave, count;
    uint16_t spawn_timer;
    int off, i;

    /* [type:1][wave:1][count:1][timer_hi:1][timer_lo:1] = 5 minimum */
    if (len < 5) return;

    wave = payload[1];
    count = payload[2];
    spawn_timer = read_u16(&payload[3]);

    /* Clear all existing asteroids */
    memset(g_Disasteroids, 0, sizeof(g_Disasteroids));

    g_Game.wave = wave;
    g_Game.disasteroidSpawnFrames = spawn_timer;

    /* Spawn asteroids from server data */
    off = 5;
    for (i = 0; i < count && off + 18 <= len; i++)
    {
        /* [x:4][y:4][dx:4][dy:4][size:1][type:1] = 18 per asteroid */
        if (i < MAX_DISASTEROIDS)
        {
            spawnDisasteroidFromServer(
                i,
                (jo_fixed)read_i32(&payload[off]),
                (jo_fixed)read_i32(&payload[off + 4]),
                (jo_fixed)read_i32(&payload[off + 8]),
                (jo_fixed)read_i32(&payload[off + 12]),
                payload[off + 16],
                payload[off + 17]
            );
        }
        off += 18;
    }

    /* Clear projectiles on wave change */
    initProjectiles();

    dnet_log("Wave started!");
}

static void process_player_kill(const uint8_t* payload, int len)
{
    uint8_t pid, lives;
    int16_t angle;
    uint16_t invuln, respawn;

    /* [type:1][player_id:1][lives:1][angle:2][invuln:2][respawn:2] = 9 */
    if (len < 9) return;

    pid = payload[1];
    lives = payload[2];
    angle = read_i16(&payload[3]);
    invuln = read_u16(&payload[5]);
    respawn = read_u16(&payload[7]);

    destroyPlayerFromServer(pid, lives, angle, invuln, respawn);
}

static void process_player_spawn(const uint8_t* payload, int len)
{
    uint8_t pid;
    int16_t angle;
    uint16_t invuln;

    /* [type:1][player_id:1][angle:2][invuln:2] = 6 */
    if (len < 6) return;

    pid = payload[1];
    angle = read_i16(&payload[2]);
    invuln = read_u16(&payload[4]);

    spawnPlayerFromServer(pid, angle, invuln);
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
    {
        /* [player_id:1][name:LP] — store game_player_id → name mapping */
        if (len >= 2) {
            uint8_t pid = payload[1];
            int slot;
            /* Find existing slot or first inactive slot */
            int target = -1;
            for (slot = 0; slot < DNET_MAX_PLAYERS; slot++) {
                if (g_net.lobby_players[slot].active &&
                    g_net.lobby_players[slot].id == pid) {
                    target = slot;
                    break;
                }
            }
            if (target < 0) {
                for (slot = 0; slot < DNET_MAX_PLAYERS; slot++) {
                    if (!g_net.lobby_players[slot].active) {
                        target = slot;
                        break;
                    }
                }
            }
            if (target >= 0) {
                g_net.lobby_players[target].id = pid;
                g_net.lobby_players[target].active = true;
                if (len >= 3) {
                    dnet_read_string(&payload[2], len - 2,
                                     g_net.lobby_players[target].name,
                                     DNET_MAX_NAME + 1);
                }
                if (target >= g_net.lobby_count)
                    g_net.lobby_count = target + 1;
            }
        }
        if (g_net.state == DNET_STATE_LOBBY)
            dnet_log("Player joined!");
        break;
    }

    case DNET_MSG_PLAYER_LEAVE:
        dnet_log("Player left!");
        break;

    case DNET_MSG_SHIP_SYNC:
        process_ship_sync(payload, len);
        break;

    case DNET_MSG_ASTEROID_SPAWN:
        process_asteroid_spawn(payload, len);
        break;

    case DNET_MSG_ASTEROID_DESTROY:
        process_asteroid_destroy(payload, len);
        break;

    case DNET_MSG_WAVE_EVENT:
        process_wave_event(payload, len);
        break;

    case DNET_MSG_PLAYER_KILL:
        process_player_kill(payload, len);
        break;

    case DNET_MSG_PLAYER_SPAWN:
        process_player_spawn(payload, len);
        break;

    case DNET_MSG_LOCAL_PLAYER_ACK:
        /* [type:1][player_id:1] — ignore provisional 0xFF */
        if (len >= 2 && payload[1] != 0xFF) {
            g_Game.myPlayerID2 = payload[1];
            dnet_log("Player 2 joined!");
        }
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

void dnet_send_ship_state(void)
{
    PPLAYER player;
    uint8_t flags;
    int len;

    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;

    /* Throttle to every 5 frames (~12fps) for smoother remote rendering */
    g_net.ship_state_cooldown++;
    if (g_net.ship_state_cooldown < 5) return;
    g_net.ship_state_cooldown = 0;

    player = &g_Players[g_net.my_player_id];
    if (player->objectState != OBJECT_STATE_ACTIVE) return;
    if (player->respawnFrames > 0) return;  /* Don't send during respawn */

    flags = 0;
    if (player->objectState == OBJECT_STATE_ACTIVE) flags |= 0x01;
    if (player->invulnerabilityFrames > 0) flags |= 0x02;
    if (player->isThrusting) flags |= 0x04;

    len = dnet_encode_ship_state(g_net.tx_buf,
        (int32_t)player->curPos.x,
        (int32_t)player->curPos.y,
        (int32_t)player->curPos.dx,
        (int32_t)player->curPos.dy,
        (int16_t)player->curPos.rot,
        flags);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_asteroid_hit(uint8_t slot, uint8_t scorer_id)
{
    int len;
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;

    len = dnet_encode_asteroid_hit(g_net.tx_buf, slot, scorer_id);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_ship_asteroid_hit(uint8_t slot, uint8_t player_id)
{
    int len;
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;

    len = dnet_encode_ship_asteroid_hit(g_net.tx_buf, slot, player_id);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_add_local_player(const char* name)
{
    int len;
    if (g_net.state != DNET_STATE_LOBBY || !g_net.transport) return;
    len = dnet_encode_add_local_player(g_net.tx_buf, name);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_add_bot(uint8_t difficulty)
{
    int len;
    if (g_net.state != DNET_STATE_LOBBY || !g_net.transport) return;
    len = dnet_encode_add_bot(g_net.tx_buf, difficulty);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_remove_bot(void)
{
    int len;
    if (g_net.state != DNET_STATE_LOBBY || !g_net.transport) return;
    len = dnet_encode_remove_bot(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_remove_local_player(void)
{
    int len;
    if (!g_net.transport) return;
    if (g_net.state != DNET_STATE_LOBBY && g_net.state != DNET_STATE_PLAYING) return;
    len = dnet_encode_remove_local_player(g_net.tx_buf);
    net_transport_send(g_net.transport, g_net.tx_buf, len);
}

void dnet_send_input_delta_p2(uint16_t frame_num, uint16_t input_bits)
{
    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;
    if (g_Game.myPlayerID2 == 0xFF) return;

    if (input_bits != g_net.last_sent_input_p2 || g_net.send_cooldown_p2 >= 15) {
        int len = dnet_encode_input_state_p2(g_net.tx_buf,
                                              g_Game.myPlayerID2,
                                              frame_num, input_bits);
        net_transport_send(g_net.transport, g_net.tx_buf, len);
        g_net.last_sent_input_p2 = input_bits;
        g_net.send_cooldown_p2 = 0;
    } else {
        g_net.send_cooldown_p2++;
    }
}

void dnet_send_ship_state_p2(void)
{
    PPLAYER player;
    uint8_t flags;
    int len;

    if (g_net.state != DNET_STATE_PLAYING || !g_net.transport) return;
    if (g_Game.myPlayerID2 == 0xFF) return;

    /* Throttle to every 5 frames (~12fps) for smoother remote rendering */
    g_net.ship_state_cooldown_p2++;
    if (g_net.ship_state_cooldown_p2 < 5) return;
    g_net.ship_state_cooldown_p2 = 0;

    player = &g_Players[g_Game.myPlayerID2];
    if (player->objectState != OBJECT_STATE_ACTIVE) return;
    if (player->respawnFrames > 0) return;  /* Don't send during respawn */

    flags = 0;
    if (player->objectState == OBJECT_STATE_ACTIVE) flags |= 0x01;
    if (player->invulnerabilityFrames > 0) flags |= 0x02;
    if (player->isThrusting) flags |= 0x04;

    len = dnet_encode_ship_state_p2(g_net.tx_buf,
        g_Game.myPlayerID2,
        (int32_t)player->curPos.x,
        (int32_t)player->curPos.y,
        (int32_t)player->curPos.dx,
        (int32_t)player->curPos.dy,
        (int16_t)player->curPos.rot,
        flags);
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
