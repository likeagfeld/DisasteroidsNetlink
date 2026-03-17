/**
 * name_entry.c - Name Entry Screen for Online Play
 *
 * Allows the player to enter a custom name using a character grid.
 * Supports A-Z, 0-9, '.', ':' (38 chars). Max name length: 16 chars.
 * D-pad navigates, A/C selects, B cancels, DEL removes, OK confirms.
 */

#include <jo/jo.h>
#include "main.h"
#include "name_entry.h"
#include "assets/assets.h"
#include "objects/star.h"

/*============================================================================
 * Character Grid Layout
 *
 * Row 0: A B C D E F G H I     (9 chars)
 * Row 1: J K L M N O P Q R     (9 chars)
 * Row 2: S T U V W X Y Z       (8 chars)
 * Row 3: 0 1 2 3 4 5 6 7 8 9   (10 chars)
 * Row 4: . : DEL OK             (4 items)
 *============================================================================*/

#define GRID_ROWS 5

static const char* g_grid_chars[] = {
    "ABCDEFGHI",   /* row 0: 9 */
    "JKLMNOPQR",   /* row 1: 9 */
    "STUVWXYZ",    /* row 2: 8 */
    "0123456789",  /* row 3: 10 */
    NULL           /* row 4: special (., :, DEL, OK) */
};

/* Row 4 has 4 items: '.', ':', DEL, OK */
#define ROW4_COUNT 4
#define ROW4_DEL   2
#define ROW4_OK    3

static int g_row_lengths[] = { 9, 9, 8, 10, ROW4_COUNT };

static char g_name_buf[DNET_MAX_NAME + 1];
static int g_name_len;
static int g_cursor_row;
static int g_cursor_col;

/*============================================================================
 * Helpers
 *============================================================================*/

static int getRowLen(int row)
{
    if (row < 0 || row >= GRID_ROWS) return 0;
    return g_row_lengths[row];
}

/* Get the character at grid position. Returns 0 for DEL/OK special items. */
static char getGridChar(int row, int col)
{
    if (row < 0 || row >= GRID_ROWS) return 0;
    if (col < 0 || col >= getRowLen(row)) return 0;

    if (row < 4) {
        return g_grid_chars[row][col];
    }

    /* Row 4 special items */
    if (col == 0) return '.';
    if (col == 1) return ':';
    return 0; /* DEL=2, OK=3 are actions */
}

/*============================================================================
 * Callbacks
 *============================================================================*/

void nameEntry_init(void)
{
    /* Pre-populate with previous name if available */
    if (g_Game.playerName[0] != '\0') {
        int i;
        g_name_len = 0;
        for (i = 0; g_Game.playerName[i] && i < DNET_MAX_NAME; i++) {
            g_name_buf[i] = g_Game.playerName[i];
            g_name_len++;
        }
        g_name_buf[g_name_len] = '\0';
    } else {
        g_name_buf[0] = '\0';
        g_name_len = 0;
    }
    g_cursor_row = 0;
    g_cursor_col = 0;

    initStars();
}

void nameEntry_input(void)
{
    int rowLen;

    if (g_Game.gameState != GAME_STATE_NAME_ENTRY) return;

    /* D-pad: move cursor */
    if (jo_is_pad1_key_pressed(JO_KEY_UP)) {
        if (g_Game.input.pressedUp == false) {
            g_cursor_row--;
            if (g_cursor_row < 0) g_cursor_row = GRID_ROWS - 1;
            /* Clamp col to new row length */
            rowLen = getRowLen(g_cursor_row);
            if (g_cursor_col >= rowLen) g_cursor_col = rowLen - 1;
        }
        g_Game.input.pressedUp = true;
    } else {
        g_Game.input.pressedUp = false;
    }

    if (jo_is_pad1_key_pressed(JO_KEY_DOWN)) {
        if (g_Game.input.pressedDown == false) {
            g_cursor_row++;
            if (g_cursor_row >= GRID_ROWS) g_cursor_row = 0;
            rowLen = getRowLen(g_cursor_row);
            if (g_cursor_col >= rowLen) g_cursor_col = rowLen - 1;
        }
        g_Game.input.pressedDown = true;
    } else {
        g_Game.input.pressedDown = false;
    }

    if (jo_is_pad1_key_pressed(JO_KEY_LEFT)) {
        if (g_Game.input.pressedLeft == false) {
            g_cursor_col--;
            if (g_cursor_col < 0) g_cursor_col = getRowLen(g_cursor_row) - 1;
        }
        g_Game.input.pressedLeft = true;
    } else {
        g_Game.input.pressedLeft = false;
    }

    if (jo_is_pad1_key_pressed(JO_KEY_RIGHT)) {
        if (g_Game.input.pressedRight == false) {
            g_cursor_col++;
            if (g_cursor_col >= getRowLen(g_cursor_row)) g_cursor_col = 0;
        }
        g_Game.input.pressedRight = true;
    } else {
        g_Game.input.pressedRight = false;
    }

    /* A or C: select character / action */
    if (jo_is_pad1_key_pressed(JO_KEY_A) ||
        jo_is_pad1_key_pressed(JO_KEY_C)) {
        if (g_Game.input.pressedAC == false) {
            if (g_cursor_row == 4 && g_cursor_col == ROW4_DEL) {
                /* DEL: remove last character */
                if (g_name_len > 0) {
                    g_name_len--;
                    g_name_buf[g_name_len] = '\0';
                }
            } else if (g_cursor_row == 4 && g_cursor_col == ROW4_OK) {
                /* OK: confirm name (minimum 1 character) */
                if (g_name_len > 0) {
                    int i;
                    /* Copy name to g_Game.playerName */
                    for (i = 0; i < g_name_len; i++)
                        g_Game.playerName[i] = g_name_buf[i];
                    g_Game.playerName[g_name_len] = '\0';

                    /* Check for second controller (port 1=multitap, port 6=Port B) */
                    g_Game.hasSecondLocal = (getP2Port() >= 0) ? true : false;
                    if (g_Game.hasSecondLocal) {
                        /* Auto-generate player 2 name = name + "2" */
                        int p2len = g_name_len;
                        for (i = 0; i < p2len && i < DNET_MAX_NAME; i++)
                            g_Game.playerName2[i] = g_name_buf[i];
                        if (p2len < DNET_MAX_NAME) {
                            g_Game.playerName2[p2len] = '2';
                            g_Game.playerName2[p2len + 1] = '\0';
                        } else {
                            /* Truncate last char and append '2' */
                            g_Game.playerName2[DNET_MAX_NAME - 1] = '2';
                            g_Game.playerName2[DNET_MAX_NAME] = '\0';
                        }
                    }

                    g_Game.myPlayerID2 = 0xFF; /* no second player yet */
                    g_Game.isOnlineMode = true;
                    transitionState(GAME_STATE_CONNECTING);
                }
            } else {
                /* Regular character */
                char ch = getGridChar(g_cursor_row, g_cursor_col);
                if (ch != 0 && g_name_len < DNET_MAX_NAME) {
                    g_name_buf[g_name_len] = ch;
                    g_name_len++;
                    g_name_buf[g_name_len] = '\0';
                }
            }
        }
        g_Game.input.pressedAC = true;
    } else {
        g_Game.input.pressedAC = false;
    }

    /* B: cancel back to title screen */
    if (jo_is_pad1_key_pressed(JO_KEY_B)) {
        if (g_Game.input.pressedB == false) {
            transitionState(GAME_STATE_TITLE_SCREEN);
        }
        g_Game.input.pressedB = true;
    } else {
        g_Game.input.pressedB = false;
    }
}

void nameEntry_update(void)
{
    if (g_Game.gameState != GAME_STATE_NAME_ENTRY) return;
    updateStars();
}

void nameEntry_draw(void)
{
    int color;
    int xPos, yPos;
    int letterSpacing;
    int xScale, yScale;
    int row, col;
    int gridX, gridY;

    if (g_Game.gameState != GAME_STATE_NAME_ENTRY) return;

    drawStars();

    color = g_Game.hudColor;

    /* Title: "ENTER NAME" using drawLetter (3D polygon font) */
    xPos = -55;
    yPos = -70;
    xScale = 2;
    yScale = 3;
    letterSpacing = 12;

    drawLetter('E', color, xPos + letterSpacing * 0, yPos, xScale, yScale);
    drawLetter('N', color, xPos + letterSpacing * 1, yPos, xScale, yScale);
    drawLetter('T', color, xPos + letterSpacing * 2, yPos, xScale, yScale);
    drawLetter('E', color, xPos + letterSpacing * 3, yPos, xScale, yScale);
    drawLetter('R', color, xPos + letterSpacing * 4, yPos, xScale, yScale);
    drawLetter('N', color, xPos + letterSpacing * 6, yPos, xScale, yScale);
    drawLetter('A', color, xPos + letterSpacing * 7, yPos, xScale, yScale);
    drawLetter('M', color, xPos + letterSpacing * 8, yPos, xScale, yScale);
    drawLetter('E', color, xPos + letterSpacing * 9, yPos, xScale, yScale);

    /* Character grid using jo_printf (VDP2 text) */
    gridX = 6;
    gridY = 9;

    /* Row 0: A-I */
    jo_printf(gridX, gridY, "A B C D E F G H I");
    /* Row 1: J-R */
    jo_printf(gridX, gridY + 2, "J K L M N O P Q R");
    /* Row 2: S-Z */
    jo_printf(gridX, gridY + 4, "S T U V W X Y Z");
    /* Row 3: 0-9 */
    jo_printf(gridX, gridY + 6, "0 1 2 3 4 5 6 7 8 9");
    /* Row 4: special */
    jo_printf(gridX, gridY + 8, ".  :  DEL  OK");

    /* Draw cursor '>' at current row, clear others */
    for (row = 0; row < GRID_ROWS; row++) {
        int rowY = gridY + (row * 2);
        if (row == g_cursor_row) {
            jo_printf(gridX - 2, rowY, ">");
        } else {
            jo_printf(gridX - 2, rowY, " ");
        }
    }

    /* Draw '^' column caret on the blank line below each row */
    for (row = 0; row < GRID_ROWS; row++) {
        int underlineY = gridY + (row * 2) + 1;
        jo_printf(gridX - 1, underlineY, "                     ");
        if (row == g_cursor_row) {
            int cx;
            if (row < 4) {
                cx = gridX + g_cursor_col * 2;
            } else {
                /* Row 4 items: .  :  DEL  OK — column offsets */
                static const int row4_offsets[] = {0, 3, 6, 11};
                cx = gridX + row4_offsets[g_cursor_col];
            }
            jo_printf(cx, underlineY, "^");
        }
    }

    /* Show selected character/action on the right side */
    {
        int selY = gridY + (g_cursor_row * 2);
        /* Clear all selection indicators first */
        for (row = 0; row < GRID_ROWS; row++) {
            if (row != g_cursor_row)
                jo_printf(28, gridY + (row * 2), "     ");
        }
        if (g_cursor_row < 4) {
            char selChar = getGridChar(g_cursor_row, g_cursor_col);
            if (selChar) {
                jo_printf(28, selY, "[%c]  ", selChar);
            }
        } else {
            static const char* labels[] = { "[.]", "[:]", "[DEL]", "[OK]" };
            jo_printf(28, selY, "%-5s", labels[g_cursor_col]);
        }
    }

    /* Display current name using drawLetter (3D font) */
    xPos = -80;
    yPos = 60;
    xScale = 2;
    yScale = 2;
    letterSpacing = 10;

    /* "NAME:" label */
    drawLetter('N', color, xPos, yPos, xScale, yScale);
    drawLetter('A', color, xPos + letterSpacing, yPos, xScale, yScale);
    drawLetter('M', color, xPos + letterSpacing * 2, yPos, xScale, yScale);
    drawLetter('E', color, xPos + letterSpacing * 3, yPos, xScale, yScale);
    drawLetter(':', color, xPos + letterSpacing * 4, yPos, xScale, yScale);

    /* Draw entered characters */
    xPos += letterSpacing * 5 + 5;
    for (col = 0; col < g_name_len; col++) {
        drawLetter(g_name_buf[col], color, xPos + letterSpacing * col, yPos, xScale, yScale);
    }

    /* Controls hint */
    if (g_name_len > 0) {
        jo_printf(1, 26, "A/C:Select B:Back OK:Confirm");
    } else {
        jo_printf(1, 26, "A/C:Select  B:Cancel        ");
    }

    /* Second controller notice */
    if (getP2Port() >= 0) {
        jo_printf(1, 27, "2P controller detected!");
    } else {
        jo_printf(1, 27, "                       ");
    }
}
