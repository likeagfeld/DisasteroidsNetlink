/*
**   Disasteroids - 12 player Asteroids clone by Slinga
**   NetLink online multiplayer added using SNCP protocol
*/

/*
** Jo Sega Saturn Engine
** Copyright (c) 2012-2017, Johannes Fetz (johannesfetz@gmail.com)
** All rights reserved.
**
** Redistribution and use in source and binary forms, with or without
** modification, are permitted provided that the following conditions are met:
**     * Redistributions of source code must retain the above copyright
**       notice, this list of conditions and the following disclaimer.
**     * Redistributions in binary form must reproduce the above copyright
**       notice, this list of conditions and the following disclaimer in the
**       documentation and/or other materials provided with the distribution.
**     * Neither the name of the Johannes Fetz nor the
**       names of its contributors may be used to endorse or promote products
**       derived from this software without specific prior written permission.
**
** THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
** ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
** WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
** DISCLAIMED. IN NO EVENT SHALL Johannes Fetz BE LIABLE FOR ANY
** DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
** (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
** LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
** ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
** (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
** SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
*/

#include <jo/jo.h>
#include "main.h"
#include "assets/assets.h"
#include "ssmtf_logo.h"
#include "title_screen.h"
#include "gameplay.h"
#include "pause.h"
#include "name_entry.h"
#include "connecting.h"
#include "lobby.h"
#include "net/disasteroids_net.h"
#include "net/saturn_uart16550.h"

GAME g_Game = {0};
ASSETS g_Assets = {0};
PLAYER g_Players[MAX_PLAYERS] = {0};

/*============================================================================
 * Saturn UART + Transport globals (used by connecting.c)
 *============================================================================*/

saturn_uart16550_t g_uart = {0};
bool g_modem_detected = false;

static bool saturn_transport_rx_ready(void* ctx)
{
    return saturn_uart_rx_ready((saturn_uart16550_t*)ctx);
}

static uint8_t saturn_transport_rx_byte(void* ctx)
{
    saturn_uart16550_t* u = (saturn_uart16550_t*)ctx;
    return (uint8_t)saturn_uart_reg_read(u, SATURN_UART_RBR);
}

static int saturn_transport_send(void* ctx, const uint8_t* data, int len)
{
    saturn_uart16550_t* u = (saturn_uart16550_t*)ctx;
    int i;
    for (i = 0; i < len; i++) {
        if (!saturn_uart_putc(u, data[i])) return i;
    }
    return len;
}

net_transport_t g_saturn_transport = {
    saturn_transport_rx_ready,
    saturn_transport_rx_byte,
    saturn_transport_send,
    (void*)0,  /* is_connected = NULL */
    (void*)0,  /* ctx = set later to &g_uart */
};

/*============================================================================
 * Network tick callback (runs every frame, all game states)
 *============================================================================*/

void network_tick(void)
{
    dnet_tick();
}

// global callbacks, not tied to a specific game state
void abcStart_callback(void);
void changeHud_input(void);
void debug_input(void);
void debug_draw(void);

// sets up callback and starts game loop
void jo_main(void)
{
    jo_core_init(JO_COLOR_Black);

    //
    // load\init game assets
    //

    loadPCMAssets();
    loadPolygons();

    //
    // game callbacks
    //

    jo_core_add_callback(ssmtfLogo_input);
    jo_core_add_callback(ssmtfLogo_update);
    jo_core_add_callback(ssmtfLogo_draw);

    jo_core_add_callback(titleScreen_input);
    jo_core_add_callback(titleScreen_update);
    jo_core_add_callback(titleScreen_draw);

    jo_core_add_callback(nameEntry_input);
    jo_core_add_callback(nameEntry_update);
    jo_core_add_callback(nameEntry_draw);

    jo_core_add_callback(connecting_input);
    jo_core_add_callback(connecting_update);
    jo_core_add_callback(connecting_draw);

    jo_core_add_callback(lobby_input);
    jo_core_add_callback(lobby_update);
    jo_core_add_callback(lobby_draw);

    jo_core_add_callback(gameplay_input);
    jo_core_add_callback(gameplay_update);
    jo_core_add_callback(gameplay_draw);

    jo_core_add_callback(pause_input);
    jo_core_add_callback(pause_draw);

    // network tick runs every frame
    jo_core_add_callback(network_tick);

    // misc callbacks
    jo_core_add_callback(changeHud_input);

    jo_core_add_callback(debug_input);
    jo_core_add_callback(debug_draw);

    // ABC + start handler
    jo_core_set_restart_game_callback(abcStart_callback);

    //
    // initialize globals
    //

    jo_3d_camera_init(&g_Game.camera);
    jo_3d_camera_look_at(&g_Game.camera);

    g_Game.hudColor = JO_COLOR_Green;

    //
    // Initialize networking
    //

    dnet_init();

    // Set up transport context
    g_saturn_transport.ctx = &g_uart;

    // Detect modem hardware (non-blocking, does NOT dial)
    saturn_netlink_smpc_enable();
    {
        /* Try both known NetLink base addresses */
        static const struct { uint32_t base; uint32_t stride; } addrs[] = {
            { 0x25895001, 4 },
            { 0x04895001, 4 },
        };
        int i;

        g_modem_detected = false;
        for (i = 0; i < 2; i++) {
            g_uart.base = addrs[i].base;
            g_uart.stride = addrs[i].stride;
            if (saturn_uart_detect(&g_uart)) {
                g_modem_detected = true;
                break;
            }
        }
    }
    dnet_set_modem_available(g_modem_detected);

    // transition to first game state
    transitionState(GAME_STATE_SSMTF_LOGO);

    // game loop
    jo_core_run();
}

// returns to title screen if player one presses ABC+Start
void abcStart_callback(void)
{
    if(g_Game.gameState == GAME_STATE_UNINITIALIZED)
    {
        // ignore callback if not yet initialized
        return;
    }

    // don't do anything if already at the title screen
    if(g_Game.gameState != GAME_STATE_TITLE_SCREEN)
    {
        // If online, send disconnect first
        if(g_Game.isOnlineMode)
        {
            dnet_send_disconnect();
            g_Game.isOnlineMode = false;
        }

        g_Game.input.pressedStart = true;
        g_Game.input.pressedAC = true;
        g_Game.input.pressedB = true;

        transitionState(GAME_STATE_TITLE_SCREEN);

        return;
    }

    return;
}

// check if player 1 pressed Y to change the HUD
// this can occur at any time
void changeHud_input(void)
{
    // player 1 press start
    if (jo_is_pad1_key_pressed(JO_KEY_Y))
    {
        if(g_Game.input.pressedY == false)
        {
            incrementHudColor();
        }
        g_Game.input.pressedY = true;
    }
    else
    {
        g_Game.input.pressedY = false;
    }
}

// check if player 1 pressed Z to change the debug level
// this can occur at any time
void debug_input(void)
{
    // player 1 press start
    if (jo_is_pad1_key_pressed(JO_KEY_Z))
    {
        if(g_Game.input.pressedZ == false)
        {
            g_Game.debug++;

            if(g_Game.debug > MAX_DEBUG_LEVEL)
            {
                g_Game.debug = 0;
                jo_clear_screen();
            }
        }
        g_Game.input.pressedZ = true;
    }
    else
    {
        g_Game.input.pressedZ = false;
    }
}

// display FPS and polygon count
void debug_draw(void)
{
    if(g_Game.debug == 0)
    {
        return;
    }

    jo_printf(0, 27, "Polys: %d    ", jo_3d_get_polygon_count());

    jo_fixed_point_time();
    slPrintFX(delta_time, slLocate(2,28));
    jo_printf(0, 28, "Frame:");
}
