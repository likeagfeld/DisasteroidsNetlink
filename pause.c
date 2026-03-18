#include <jo/jo.h>
#include <stdlib.h>
#include "main.h"
#include "assets/assets.h"
#include "net/disasteroids_net.h"

typedef enum _PAUSE_OPTIONS
{
    PAUSE_OPTIONS_RESUME_OR_RESTART = 0,
    PAUSE_OPTIONS_QUIT = 1,
    PAUSE_OPTION_MAX,
} PAUSE_OPTIONS;

static int sortByScore(const void * player1, const void * player2);
static int sortByLives(const void * player1, const void * player2);
static void validateScores(void);

static void drawPauseLines(void);
static void drawPauseLegend(void);
static void drawPauseScore(void);
static void drawPauseScoreShip(jo_3d_mesh* mesh, int xPos, int yPos, int color);
static void drawPauseMenuCursor(void);

static void checkForPausePress(void);
static void checkForPauseMenu(void);

int g_PauseCursorColor = JO_COLOR_Green;
PAUSE_OPTIONS g_PauseChoice = 0;

//
// Pause callbacks
//

// input for handling pause
void pause_input(void)
{
    if(g_Game.gameState != GAME_STATE_GAMEPLAY)
    {
        return;
    }

    if(g_Game.isPaused == false)
    {
        // only check for pause press if the game is unpaused
        checkForPausePress();
    }
    else
    {
        // only check for pause menu presses if the game is paused
        checkForPauseMenu();
    }

    return;
}

// draw the current score while the game is paused
void pause_draw(void)
{
    if(g_Game.gameState != GAME_STATE_GAMEPLAY)
    {
        return;
    }

    if(g_Game.isPaused == false)
    {
        return;
    }

    //
    // score
    //

    drawPauseLines();
    drawPauseLegend();
    drawPauseScore();
    drawPauseMenuCursor();

    return;
}

// pause the game
void pauseGame(int track)
{
    int index = 0;

    g_Game.input.pressedLeft = true;
    g_Game.input.pressedRight = true;

    // randomly choose a color for the cursor ship
    index = jo_random(COUNTOF(g_Assets.randomizedColors)) - 1;
    g_PauseCursorColor = g_Assets.randomizedColors[index];

    g_PauseChoice = 0;

    g_Game.isPaused = true;
    playCDTrack(track);
}

//
// Pause private functions
//

// check if player 1 paused the game
static void checkForPausePress(void)
{
    // No manual pause in online mode (game-over pause is triggered by code)
    if(g_Game.isOnlineMode) return;

    // player 1 press start
    if (jo_is_pad1_key_pressed(JO_KEY_START))
    {
        if(g_Game.input.pressedStart == false)
        {
            pauseGame(PAUSE_TRACK);
        }
        g_Game.input.pressedStart = true;
    }
    else
    {
        g_Game.input.pressedStart = false;
    }
}

// pause menu options
static void checkForPauseMenu(void)
{
    int pauseChoice = g_PauseChoice;

    if (jo_is_pad1_key_pressed(JO_KEY_LEFT))
    {
        if(g_Game.input.pressedLeft == false)
        {
            pauseChoice--;
        }
        g_Game.input.pressedLeft = true;
    }
    else
    {
        g_Game.input.pressedLeft = false;
    }

    if (jo_is_pad1_key_pressed(JO_KEY_RIGHT))
    {
        if(g_Game.input.pressedRight == false)
        {
            pauseChoice++;
        }
        g_Game.input.pressedRight = true;
    }
    else
    {
        g_Game.input.pressedRight = false;
    }

    // keep pause choice in range
    if(pauseChoice < 0)
    {
        pauseChoice = PAUSE_OPTION_MAX - 1;
    }
    else if(pauseChoice >= PAUSE_OPTION_MAX)
    {
        pauseChoice = 0;
    }

    g_PauseChoice = pauseChoice;

    if (jo_is_pad1_key_pressed(JO_KEY_START))
    {
        if(g_Game.input.pressedStart == false)
        {
            switch(pauseChoice)
            {
                case PAUSE_OPTIONS_RESUME_OR_RESTART:
                {
                    // the leftmost option can restart the game on game over
                    // or unpause if paused depending on the game state
                    if(g_Game.isGameOver == true)
                    {
                        if(g_Game.isOnlineMode)
                        {
                            // Online: return to lobby
                            g_Game.isGameOver = false;
                            g_Game.isPaused = false;
                            transitionState(GAME_STATE_LOBBY);
                        }
                        else
                        {
                            transitionState(GAME_STATE_GAMEPLAY);
                        }
                        break;
                    }
                    else
                    {
                        // simply unpause
                        g_Game.isPaused = false;
                        break;
                    }
                }
                case PAUSE_OPTIONS_QUIT:
                {
                    // rightmost option always quits to the title screen
                    if(g_Game.isOnlineMode && g_Game.isGameOver)
                    {
                        g_Game.isGameOver = false;
                        g_Game.isPaused = false;
                    }
                    transitionState(GAME_STATE_TITLE_SCREEN);
                    break;
                }
            }
        }

        g_Game.input.pressedStart = true;
        return;
    }
    else
    {
        g_Game.input.pressedStart = false;
    }

    return;
}

// normalize scores before displaying in game over or pause screen
static void validateScores(void)
{
    for(int i = 0; i < MAX_PLAYERS; i++)
    {
        // normalize score data
        g_Players[i].score.wave = g_Players[i].score.wave % 100;
        g_Players[i].score.points = g_Players[i].score.points % 1000;
    }

    return;
}

// sort players by score for coop mode
static int sortByScore(const void * player1, const void * player2)
{
    PPLAYER a = (PPLAYER)player1;
    PPLAYER b = (PPLAYER)player2;

    // sort by score
    if(b->score.points != a->score.points)
    {
        if(b->score.points > a->score.points)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // sort by wave
    if(b->score.wave != a->score.wave)
    {
        if(b->score.wave > a->score.wave)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // sort by lives
    if(b->numLives != a->numLives)
    {
        if(b->numLives > a->numLives)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // tie-breaker: lower player ID wins
    if(b->playerID < a->playerID)
    {
        return 1;
    }
    else
    {
        return -1;
    }

    return 0;
}

// sort players by lives for versus mode
static int sortByLives(const void * player1, const void * player2)
{
    PPLAYER a = (PPLAYER)player1;
    PPLAYER b = (PPLAYER)player2;

    // sort by lives
    if(b->numLives != a->numLives)
    {
        if(b->numLives > a->numLives)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // sort by wave
    if(b->score.wave != a->score.wave)
    {
        if(b->score.wave > a->score.wave)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // sort by score
    if(b->score.points != a->score.points)
    {
        if(b->score.points > a->score.points)
        {
            return 1;
        }
        else
        {
            return -1;
        }
    }

    // tie-breaker: lower player ID wins
    if(b->playerID < a->playerID)
    {
        return 1;
    }
    else
    {
        return -1;
    }

    return 0;
}

// vertical and horizontal lines framing the scores
static void drawPauseLines(void)
{
    jo_3d_mesh* mesh = NULL;
    int xPos = 0;
    int yPos = 0;

    // lines
    mesh = g_Assets.verticalLine;

    xPos = 63;
    jo_3d_push_matrix();
    {
        jo_3d_camera_look_at(&g_Game.camera);
        jo_3d_set_mesh_color(mesh, g_Game.hudColor);
        jo_3d_translate_matrix(xPos, yPos, 0);
    }
    jo_3d_pop_matrix();

    mesh = g_Assets.horizontalLine;

    xPos = 0;
    yPos = -86;
    jo_3d_push_matrix();
    {
        jo_3d_camera_look_at(&g_Game.camera);
        jo_3d_set_mesh_color(mesh, g_Game.hudColor);
        jo_3d_translate_matrix(xPos, yPos, 0);
        jo_3d_mesh_draw(mesh);
    }
    jo_3d_pop_matrix();
}

// player ships in the score
static void drawPauseScoreShip(jo_3d_mesh* mesh, int xPos, int yPos, int color)
{
    jo_3d_push_matrix();
    {
        jo_3d_camera_look_at(&g_Game.camera);
        jo_3d_set_mesh_color(mesh, color); // todo
        jo_3d_translate_matrix(xPos + 12, yPos - 2, 0);
        jo_3d_rotate_matrix(0, 0, 90);
        jo_3d_mesh_draw(mesh);
    }
    jo_3d_pop_matrix();
}

// look up a player name by game_player_id from game roster
// (game_roster survives lobby state transitions, unlike lobby_players)
static const char* getOnlinePlayerName(int id)
{
    const dnet_state_data_t* nd = dnet_get_data();
    int i;
    for (i = 0; i < nd->game_roster_count && i < DNET_MAX_PLAYERS; i++) {
        if (nd->game_roster[i].active && nd->game_roster[i].id == (uint8_t)id)
            return nd->game_roster[i].name;
    }
    return "";
}

// draw a string using drawLetter, max len chars
static void drawString(const char* str, int maxLen, int color,
                       int xPos, int yPos, int xScale, int yScale, int spacing)
{
    int i;
    for (i = 0; i < maxLen && str[i] != '\0'; i++) {
        char ch = str[i];
        /* drawLetter handles A-Z, 0-9, and some symbols */
        drawLetter(ch, color, xPos + (spacing * i), yPos, xScale, yScale);
    }
}

/*
 * Column positions for the score table.
 * Offline: 5 columns (RANK, SHIP, LIVES, WAVES, SCORE) centered on screen.
 * Online:  6 columns (RANK, SHIP, NAME, LIVES, WAVES, SCORE) centered on screen.
 */

/* Offline column positions (original layout, centered) */
#define COL_OFF_RANK   (-101)
#define COL_OFF_SHIP   (-61)
#define COL_OFF_LIVES  (-21)
#define COL_OFF_WAVES  (25)
#define COL_OFF_SCORE  (71)

/* Online column positions (with NAME column, re-centered) */
#define COL_ON_RANK    (-108)
#define COL_ON_SHIP    (-78)
#define COL_ON_NAME    (-48)
#define COL_ON_LIVES   (6)
#define COL_ON_WAVES   (42)
#define COL_ON_SCORE   (78)

// legend for the score chart
static void drawPauseLegend(void)
{
    int xScale = 1;
    int yScale = 1;
    int letterSpacing = 6;
    int xPos = 0;
    int yPos = -90;
    int color = g_Game.hudColor;
    bool online = g_Game.isOnlineMode;

    //
    // Legend: Rank, Ship, [Name], Lives, Waves, Score
    //

    xPos = online ? COL_ON_RANK : COL_OFF_RANK;
    drawLetter('R', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('A', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('N', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('K', color, xPos + (letterSpacing*3), yPos, xScale, yScale);

    xPos = online ? COL_ON_SHIP : COL_OFF_SHIP;
    drawLetter('S', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('H', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('I', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('P', color, xPos + (letterSpacing*3), yPos, xScale, yScale);

    if (online) {
        xPos = COL_ON_NAME;
        drawLetter('N', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
        drawLetter('A', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
        drawLetter('M', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter('E', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
    }

    xPos = online ? COL_ON_LIVES : COL_OFF_LIVES;
    drawLetter('L', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('I', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('V', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('E', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
    drawLetter('S', color, xPos + (letterSpacing*4), yPos, xScale, yScale);

    xPos = online ? COL_ON_WAVES : COL_OFF_WAVES;
    drawLetter('W', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('A', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('V', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('E', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
    drawLetter('S', color, xPos + (letterSpacing*4), yPos, xScale, yScale);

    xPos = online ? COL_ON_SCORE : COL_OFF_SCORE;
    drawLetter('S', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('C', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('O', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('R', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
    drawLetter('E', color, xPos + (letterSpacing*4), yPos, xScale, yScale);

    //
    // Options at the bottom of the screen
    //

    yPos = 92;
    xPos = -64;

    if(g_Game.isGameOver == false)
    {
        drawLetter('U', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
        drawLetter('N', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
        drawLetter('P', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter('A', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
        drawLetter('U', color, xPos + (letterSpacing*4), yPos, xScale, yScale);
        drawLetter('S', color, xPos + (letterSpacing*5), yPos, xScale, yScale);
        drawLetter('E', color, xPos + (letterSpacing*6), yPos, xScale, yScale);
    }
    else if(g_Game.isOnlineMode)
    {
        drawLetter('L', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
        drawLetter('O', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
        drawLetter('B', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter('B', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
        drawLetter('Y', color, xPos + (letterSpacing*4), yPos, xScale, yScale);
    }
    else
    {
        drawLetter('R', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
        drawLetter('E', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
        drawLetter('S', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter('T', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
        drawLetter('A', color, xPos + (letterSpacing*4), yPos, xScale, yScale);
        drawLetter('R', color, xPos + (letterSpacing*5), yPos, xScale, yScale);
        drawLetter('T', color, xPos + (letterSpacing*6), yPos, xScale, yScale);
    }

    xPos = 64;

    drawLetter('Q', color, xPos + (letterSpacing*0), yPos, xScale, yScale);
    drawLetter('U', color, xPos + (letterSpacing*1), yPos, xScale, yScale);
    drawLetter('I', color, xPos + (letterSpacing*2), yPos, xScale, yScale);
    drawLetter('T', color, xPos + (letterSpacing*3), yPos, xScale, yScale);
}

// pause scores
static void drawPauseScore(void)
{
    PLAYER sortedPlayers[MAX_PLAYERS] = {0};
    jo_3d_mesh* mesh = NULL;
    int nonPlayerCount = 0;
    int color = 0;
    int xPos = 0;
    int yPos = 0;
    int xScale = 1;
    int yScale = 1;
    int letterSpacing = 6;
    bool online = g_Game.isOnlineMode;

    validateScores();
    memcpy(sortedPlayers, g_Players, sizeof(sortedPlayers));

    if(g_Game.gameType == GAME_TYPE_VERSUS)
    {
        // rank by lives
        qsort(sortedPlayers, COUNTOF(sortedPlayers), sizeof(PLAYER), sortByLives);
    }
    else
    {
        // rank by score
        qsort(sortedPlayers, COUNTOF(sortedPlayers), sizeof(PLAYER), sortByScore);
    }

    color = g_Game.hudColor;
    yPos = -90;

    // loop through all the sorted players displaying the score
    for(unsigned int i = 0; i < COUNTOF(sortedPlayers); i++)
    {
        unsigned char digit1 = 0;
        unsigned char digit2 = 0;
        unsigned char digit3 = 0;
        unsigned int lives = 0;
        unsigned int waves = 0;
        unsigned int score = 0;
        PPLAYER player = NULL;
        int shipColor = 0;

        player = &sortedPlayers[i];

        yPos += 14;

        // rank
        xPos = online ? COL_ON_RANK : COL_OFF_RANK;

        digit1 = ((i+1) / 10) + '0';
        digit2 = ((i+1) % 10) + '0';

        drawLetter(digit1, color, xPos + (letterSpacing*1), yPos, xScale, yScale);
        drawLetter(digit2, color, xPos + (letterSpacing*2), yPos, xScale, yScale);

        // player ship — use playerID as mesh index, not rank position
        mesh = g_Assets.ships[player->playerID % MAX_PLAYERS];

        xPos = online ? COL_ON_SHIP : COL_OFF_SHIP;

        // For active player, should be actual color
        // for non-player should be randomizedColor
        if(player->color != 0)
        {
            shipColor = player->color;
        }
        else
        {
            // non-player, grab a color from sortedColors array
            shipColor = getPlayerColorByIndex(nonPlayerCount);
            nonPlayerCount++;
        }

        drawPauseScoreShip(mesh, xPos, yPos, shipColor);

        // name (online only) — drawn in player's ship color for clear association
        if (online)
        {
            const char* name = getOnlinePlayerName(player->playerID);
            xPos = COL_ON_NAME;
            drawString(name, 8, shipColor, xPos, yPos, xScale, yScale, letterSpacing);
        }

        // lives
        xPos = online ? (COL_ON_LIVES + 3) : -24;

        lives = player->numLives % 100;

        digit1 = (lives / 10) + '0';
        digit2 = (lives % 10) + '0';

        drawLetter(digit1, color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter(digit2, color, xPos + (letterSpacing*3), yPos, xScale, yScale);

        // wave
        waves = player->score.wave % 100;

        xPos = online ? (COL_ON_WAVES + 3) : 22;
        digit1 = (waves / 10) + '0';
        digit2 = (waves % 10) + '0';

        drawLetter(digit1, color, xPos + (letterSpacing*2), yPos, xScale, yScale);
        drawLetter(digit2, color, xPos + (letterSpacing*3), yPos, xScale, yScale);

        // points
        score = player->score.points % 1000;

        xPos = online ? COL_ON_SCORE : 71;
        digit1 = (score / 100) + '0';
        digit2 = ((score - ((score/100) * 100))/10) + '0';
        digit3 = (score % 10) + '0';

        drawLetter(digit1, color, xPos + letterSpacing*1, yPos, xScale, yScale);
        drawLetter(digit2, color, xPos + letterSpacing*2, yPos, xScale, yScale);
        drawLetter(digit3, color, xPos + letterSpacing*3, yPos, xScale, yScale);
    }
}

// cursor ship
static void drawPauseMenuCursor(void)
{
    int color = g_PauseCursorColor;
    int xPos = -72 + (g_PauseChoice * 128);
    int yPos = 91;

    jo_3d_mesh* mesh = g_Assets.cursorShip;

    jo_3d_push_matrix();
    {
        jo_3d_camera_look_at(&g_Game.camera);
        jo_3d_set_mesh_color(mesh, color);
        jo_3d_translate_matrix(xPos, yPos, 0);
        jo_3d_rotate_matrix(0, 0, 90);
        jo_3d_mesh_draw(mesh);
    }
    jo_3d_pop_matrix();

    return;
}
