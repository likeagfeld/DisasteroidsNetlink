/**
 * saturn_uart16550.h - 16550 UART Driver for Saturn NetLink Modem
 *
 * The Saturn NetLink modem (MK-80118) exposes a 16550-compatible UART on
 * the A-bus (cartridge slot). The modem contains a Rockwell L39 controller
 * (6502-based) connected to an RC288DPi V.34 data pump. The L39 presents
 * standard Hayes AT commands over the UART interface — no proprietary
 * protocol is needed.
 *
 * IMPORTANT: Two Saturn-specific quirks are required for correct operation:
 *
 * 1. SMPC command 0x0A (NEON) must be sent to power on the modem before
 *    any register access. Without this, the hardware is unpowered.
 *    Call saturn_netlink_smpc_enable() before using any UART functions.
 *
 * 2. After each register read or write, 0xFF must be written to address
 *    0x2582503D. The purpose is undocumented but required on real hardware.
 *    This is handled automatically by saturn_uart_reg_read/write().
 *
 * Register base: 0x25895001 (stride 4, odd-byte addresses)
 * Interrupt: SCU External Interrupt 12 (vector 0x5C)
 *
 * Sources:
 *   - Yabause/Kronos netlink.c: register map and AT command handling
 *     https://github.com/Yabause/yabause/blob/master/yabause/src/netlink.c
 *   - CyberWarriorX (Theo Berkau) on SegaXtreme: SMPC enable + quirk addr
 *     https://segaxtreme.net/threads/yabause-netlink-code.24153/
 *   - SegaXtreme NetLink ROM dumps thread: L39 and hardware details
 *     https://segaxtreme.net/threads/netlink-rom-dumps.24942/
 *   - Yabause Wiki SMPC page: command 0x0A = NEON, 0x0B = NEOFF
 *     http://wiki.yabause.org/index.php5?title=SMPC
 */

#ifndef SATURN_UART16550_H
#define SATURN_UART16550_H

#include <stdint.h>
#include <stdbool.h>

/* =========================================================================
 * UART Instance — configurable base address and stride
 * ========================================================================= */

typedef struct {
    uint32_t base;    /* Register base address (e.g. 0x25895001) */
    uint32_t stride;  /* Bytes between registers (e.g. 4) */
} saturn_uart16550_t;

/* =========================================================================
 * SMPC Modem Power Control
 *
 * The NetLink modem must be powered on via SMPC before any UART register
 * access. SMPC command 0x0A (NEON) enables the modem; 0x0B (NEOFF)
 * disables it. These follow the same ON=even/OFF=odd pattern as SNDON
 * (0x06)/SNDOFF (0x07) and CDON (0x08)/CDOFF (0x09).
 *
 * Source: CyberWarriorX, SegaXtreme "Yabause Netlink Code" thread
 *         https://segaxtreme.net/threads/yabause-netlink-code.24153/
 * ========================================================================= */

#define SATURN_SMPC_COMREG  (*(volatile uint8_t*)0x2010001F)
#define SATURN_SMPC_SF      (*(volatile uint8_t*)0x20100063)

#define SATURN_SMPC_CMD_NEON   0x0A  /* NetLink Enable ON  */
#define SATURN_SMPC_CMD_NEOFF  0x0B  /* NetLink Enable OFF */

/**
 * Send an SMPC command and wait for completion.
 */
static inline void saturn_smpc_command(uint8_t cmd) {
    while (SATURN_SMPC_SF & 0x01);  /* Wait for SMPC ready */
    SATURN_SMPC_SF = 0x01;
    SATURN_SMPC_COMREG = cmd;
    while (SATURN_SMPC_SF & 0x01);  /* Wait for completion */
}

/**
 * Power on the NetLink modem via SMPC.
 * Must be called before any UART register access.
 * Includes a settling delay for the L39 controller to boot from EEPROM.
 */
static inline void saturn_netlink_smpc_enable(void) {
    /* Disable first for a clean power cycle */
    saturn_smpc_command(SATURN_SMPC_CMD_NEOFF);
    for (volatile uint32_t i = 0; i < 100000; i++);

    /* Enable modem */
    saturn_smpc_command(SATURN_SMPC_CMD_NEON);

    /* L39 boots from 128Kx8 EEPROM (MX27C1000) — give it time */
    for (volatile uint32_t i = 0; i < 2000000; i++);
}

/**
 * Power off the NetLink modem via SMPC.
 */
static inline void saturn_netlink_smpc_disable(void) {
    saturn_smpc_command(SATURN_SMPC_CMD_NEOFF);
}

/* =========================================================================
 * Register Access
 *
 * Saturn-specific quirk: after each UART register access (read or write),
 * 0xFF must be written to address 0x2582503D. This address appears in the
 * Yabause NetlinkWriteByte handler (case 0x2503D, marked "???") and was
 * documented by CyberWarriorX on SegaXtreme. The exact purpose is unknown
 * but omitting it causes communication failures on real hardware.
 *
 * Source: CyberWarriorX, SegaXtreme "Yabause Netlink Code" thread
 *         https://segaxtreme.net/threads/yabause-netlink-code.24153/
 * ========================================================================= */

/**
 * Post-access quirk address. Write 0xFF here after every register access.
 */
#define SATURN_NETLINK_QUIRK_ADDR  (*(volatile uint8_t*)0x2582503D)

/**
 * Perform the required post-access write.
 */
#define SATURN_NETLINK_POST_ACCESS() \
    do { SATURN_NETLINK_QUIRK_ADDR = 0xFF; } while (0)

/**
 * Raw register address (no quirk). Use saturn_uart_reg_read/write instead
 * for normal operation. This macro is provided for detection probing where
 * the quirk address itself might not be mapped.
 */
#define SATURN_UART_REG_RAW(u, n) \
    (*(volatile uint8_t*)((u)->base + (uint32_t)(n) * (u)->stride))

/**
 * Read a UART register with post-access quirk.
 */
static inline uint8_t saturn_uart_reg_read(const saturn_uart16550_t* uart,
                                            int reg) {
    uint8_t val = SATURN_UART_REG_RAW(uart, reg);
    SATURN_NETLINK_POST_ACCESS();
    return val;
}

/**
 * Write a UART register with post-access quirk.
 */
static inline void saturn_uart_reg_write(const saturn_uart16550_t* uart,
                                          int reg, uint8_t val) {
    SATURN_UART_REG_RAW(uart, reg) = val;
    SATURN_NETLINK_POST_ACCESS();
}

/* Backward-compatible macro — prefer saturn_uart_reg_read/write */
#define SATURN_UART_REG(u, n)  SATURN_UART_REG_RAW(u, n)

/* Register indices */
#define SATURN_UART_RBR  0   /* Receive Buffer Register (read)           */
#define SATURN_UART_THR  0   /* Transmit Holding Register (write)        */
#define SATURN_UART_DLL  0   /* Divisor Latch Low (when DLAB=1)          */
#define SATURN_UART_IER  1   /* Interrupt Enable (or DLM when DLAB=1)    */
#define SATURN_UART_DLM  1   /* Divisor Latch High (when DLAB=1)         */
#define SATURN_UART_IIR  2   /* Interrupt Identification (read)          */
#define SATURN_UART_FCR  2   /* FIFO Control Register (write)            */
#define SATURN_UART_LCR  3   /* Line Control Register                    */
#define SATURN_UART_MCR  4   /* Modem Control Register                   */
#define SATURN_UART_LSR  5   /* Line Status Register                     */
#define SATURN_UART_MSR  6   /* Modem Status Register                    */
#define SATURN_UART_SCR  7   /* Scratch Register                         */

/* =========================================================================
 * LSR - Line Status Register bits
 * ========================================================================= */

#define SATURN_UART_LSR_DR    0x01  /* Data Ready */
#define SATURN_UART_LSR_OE    0x02  /* Overrun Error */
#define SATURN_UART_LSR_PE    0x04  /* Parity Error */
#define SATURN_UART_LSR_FE    0x08  /* Framing Error */
#define SATURN_UART_LSR_BI    0x10  /* Break Interrupt */
#define SATURN_UART_LSR_THRE  0x20  /* THR Empty */
#define SATURN_UART_LSR_TEMT  0x40  /* Transmitter Empty */
#define SATURN_UART_LSR_ERR   0x80  /* Error in FIFO */

/* =========================================================================
 * LCR - Line Control Register bits
 * ========================================================================= */

#define SATURN_UART_LCR_WLS0  0x01  /* Word Length Select bit 0 */
#define SATURN_UART_LCR_WLS1  0x02  /* Word Length Select bit 1 */
#define SATURN_UART_LCR_STB   0x04  /* Stop Bits (0=1, 1=2) */
#define SATURN_UART_LCR_PEN   0x08  /* Parity Enable */
#define SATURN_UART_LCR_EPS   0x10  /* Even Parity Select */
#define SATURN_UART_LCR_BRK   0x40  /* Break Control */
#define SATURN_UART_LCR_DLAB  0x80  /* Divisor Latch Access Bit */

/* 8N1 = WLS1|WLS0 (8-bit), no parity, 1 stop bit */
#define SATURN_UART_LCR_8N1   (SATURN_UART_LCR_WLS0 | SATURN_UART_LCR_WLS1)

/* =========================================================================
 * MCR - Modem Control Register bits
 * ========================================================================= */

#define SATURN_UART_MCR_DTR   0x01  /* Data Terminal Ready */
#define SATURN_UART_MCR_RTS   0x02  /* Request To Send */
#define SATURN_UART_MCR_OUT1  0x04  /* Output 1 */
#define SATURN_UART_MCR_OUT2  0x08  /* Output 2 (enables interrupts) */
#define SATURN_UART_MCR_LOOP  0x10  /* Loopback mode */

/* =========================================================================
 * MSR - Modem Status Register bits
 * ========================================================================= */

#define SATURN_UART_MSR_DCTS  0x01  /* Delta CTS */
#define SATURN_UART_MSR_DDSR  0x02  /* Delta DSR */
#define SATURN_UART_MSR_TERI  0x04  /* Trailing Edge RI */
#define SATURN_UART_MSR_DDCD  0x08  /* Delta DCD */
#define SATURN_UART_MSR_CTS   0x10  /* Clear To Send */
#define SATURN_UART_MSR_DSR   0x20  /* Data Set Ready */
#define SATURN_UART_MSR_RI    0x40  /* Ring Indicator */
#define SATURN_UART_MSR_DCD   0x80  /* Data Carrier Detect */

/* =========================================================================
 * FCR - FIFO Control Register bits
 * ========================================================================= */

#define SATURN_UART_FCR_ENABLE   0x01  /* Enable FIFOs */
#define SATURN_UART_FCR_RXRESET  0x02  /* Reset RX FIFO */
#define SATURN_UART_FCR_TXRESET  0x04  /* Reset TX FIFO */

/* Enable + clear both FIFOs */
#define SATURN_UART_FCR_INIT \
    (SATURN_UART_FCR_ENABLE | SATURN_UART_FCR_RXRESET | SATURN_UART_FCR_TXRESET)

/* =========================================================================
 * IIR - Interrupt Identification Register bits
 * ========================================================================= */

#define SATURN_UART_IIR_NOINT  0x01  /* No interrupt pending */

/* =========================================================================
 * Detection
 *
 * Detection uses SATURN_UART_REG_RAW (no post-access quirk) because
 * during probing we don't yet know if the quirk address is valid. Once
 * a UART is found and selected, all subsequent access uses the
 * saturn_uart_reg_read/write functions which include the quirk.
 * ========================================================================= */

/**
 * Detect a 16550 UART at the configured base address.
 *
 * Tests:
 * 1. LSR must not be 0xFF (open bus / no device)
 * 2. Scratch register write/readback must match
 *
 * @return true if a 16550 appears to be present
 */
static inline bool saturn_uart_detect(const saturn_uart16550_t* uart) {
    uint8_t lsr, scr_read;

    /* Read LSR — open bus reads 0xFF on Saturn */
    lsr = SATURN_UART_REG_RAW(uart, SATURN_UART_LSR);
    if (lsr == 0xFF) return false;

    /* Scratch register write/readback test */
    SATURN_UART_REG_RAW(uart, SATURN_UART_SCR) = 0xA5;
    for (volatile int i = 0; i < 100; i++); /* brief settle */
    scr_read = SATURN_UART_REG_RAW(uart, SATURN_UART_SCR);
    if (scr_read != 0xA5) return false;

    SATURN_UART_REG_RAW(uart, SATURN_UART_SCR) = 0x5A;
    for (volatile int i = 0; i < 100; i++);
    scr_read = SATURN_UART_REG_RAW(uart, SATURN_UART_SCR);
    if (scr_read != 0x5A) return false;

    return true;
}

/**
 * Extended detection that logs individual register values.
 * Fills result fields for diagnostic display.
 */
typedef struct {
    uint8_t lsr;
    uint8_t msr;
    uint8_t iir;
    uint8_t scr_a5;    /* readback after writing 0xA5 */
    uint8_t scr_5a;    /* readback after writing 0x5A */
    bool    detected;
} saturn_uart_detect_result_t;

static inline saturn_uart_detect_result_t
saturn_uart_detect_verbose(const saturn_uart16550_t* uart) {
    saturn_uart_detect_result_t r;

    r.lsr = SATURN_UART_REG_RAW(uart, SATURN_UART_LSR);
    r.msr = SATURN_UART_REG_RAW(uart, SATURN_UART_MSR);
    r.iir = SATURN_UART_REG_RAW(uart, SATURN_UART_IIR);

    SATURN_UART_REG_RAW(uart, SATURN_UART_SCR) = 0xA5;
    for (volatile int i = 0; i < 100; i++);
    r.scr_a5 = SATURN_UART_REG_RAW(uart, SATURN_UART_SCR);

    SATURN_UART_REG_RAW(uart, SATURN_UART_SCR) = 0x5A;
    for (volatile int i = 0; i < 100; i++);
    r.scr_5a = SATURN_UART_REG_RAW(uart, SATURN_UART_SCR);

    r.detected = (r.lsr != 0xFF) && (r.scr_a5 == 0xA5) && (r.scr_5a == 0x5A);
    return r;
}

/* =========================================================================
 * Initialization
 * ========================================================================= */

/**
 * Set baud rate via the 16550 divisor latch.
 * @param divisor  16-bit divisor value (DLL:DLM)
 */
static inline void saturn_uart_set_baud(const saturn_uart16550_t* uart,
                                         uint16_t divisor) {
    uint8_t lcr = saturn_uart_reg_read(uart, SATURN_UART_LCR);

    /* Set DLAB to access divisor latch */
    saturn_uart_reg_write(uart, SATURN_UART_LCR, lcr | SATURN_UART_LCR_DLAB);

    /* Write divisor */
    saturn_uart_reg_write(uart, SATURN_UART_DLL, (uint8_t)(divisor & 0xFF));
    saturn_uart_reg_write(uart, SATURN_UART_DLM, (uint8_t)((divisor >> 8) & 0xFF));

    /* Clear DLAB */
    saturn_uart_reg_write(uart, SATURN_UART_LCR, lcr & ~SATURN_UART_LCR_DLAB);
}

/**
 * Initialize the 16550 UART for 8N1 communication.
 * @param divisor  Baud rate divisor (0 = don't change baud)
 */
static inline void saturn_uart_init(const saturn_uart16550_t* uart,
                                     uint16_t divisor) {
    /* Disable interrupts */
    saturn_uart_reg_write(uart, SATURN_UART_IER, 0x00);

    /* Set 8N1 line format */
    saturn_uart_reg_write(uart, SATURN_UART_LCR, SATURN_UART_LCR_8N1);

    /* Set baud rate if requested */
    if (divisor > 0) {
        saturn_uart_set_baud(uart, divisor);
    }

    /* Enable and clear FIFOs */
    saturn_uart_reg_write(uart, SATURN_UART_FCR, SATURN_UART_FCR_INIT);

    /* Assert DTR, RTS, and OUT2 — L39 controller may need OUT2 as host-ready */
    saturn_uart_reg_write(uart, SATURN_UART_MCR,
        SATURN_UART_MCR_DTR | SATURN_UART_MCR_RTS | SATURN_UART_MCR_OUT2);

    /* Stabilization delay — modem needs time after DTR/RTS assertion */
    for (volatile int i = 0; i < 50000; i++);
}

/* =========================================================================
 * Status
 * ========================================================================= */

/**
 * Check if TX holding register is empty (ready to send).
 */
static inline bool saturn_uart_tx_ready(const saturn_uart16550_t* uart) {
    return (saturn_uart_reg_read(uart, SATURN_UART_LSR) &
            SATURN_UART_LSR_THRE) != 0;
}

/**
 * Check if received data is available.
 */
static inline bool saturn_uart_rx_ready(const saturn_uart16550_t* uart) {
    return (saturn_uart_reg_read(uart, SATURN_UART_LSR) &
            SATURN_UART_LSR_DR) != 0;
}

/**
 * Check for receive errors (overrun, parity, framing, break).
 */
static inline bool saturn_uart_rx_error(const saturn_uart16550_t* uart) {
    return (saturn_uart_reg_read(uart, SATURN_UART_LSR) &
            (SATURN_UART_LSR_OE | SATURN_UART_LSR_PE |
             SATURN_UART_LSR_FE | SATURN_UART_LSR_BI)) != 0;
}

/**
 * Clear error conditions by reading LSR and RBR.
 */
static inline void saturn_uart_clear_errors(const saturn_uart16550_t* uart) {
    (void)saturn_uart_reg_read(uart, SATURN_UART_LSR);
    (void)saturn_uart_reg_read(uart, SATURN_UART_RBR);
}

/**
 * Read raw LSR value (for diagnostics).
 */
static inline uint8_t saturn_uart_read_lsr(const saturn_uart16550_t* uart) {
    return saturn_uart_reg_read(uart, SATURN_UART_LSR);
}

/**
 * Read raw MSR value (for diagnostics).
 */
static inline uint8_t saturn_uart_read_msr(const saturn_uart16550_t* uart) {
    return saturn_uart_reg_read(uart, SATURN_UART_MSR);
}

/**
 * Read raw IIR value (for diagnostics).
 */
static inline uint8_t saturn_uart_read_iir(const saturn_uart16550_t* uart) {
    return saturn_uart_reg_read(uart, SATURN_UART_IIR);
}

/* =========================================================================
 * Byte I/O
 * ========================================================================= */

/**
 * Send a single byte (blocking with timeout).
 * @return true if sent, false on timeout
 */
static inline bool saturn_uart_putc(const saturn_uart16550_t* uart,
                                     uint8_t c) {
    uint32_t timeout = 200000;
    while (!saturn_uart_tx_ready(uart)) {
        if (--timeout == 0) return false;
    }
    saturn_uart_reg_write(uart, SATURN_UART_THR, c);
    return true;
}

/**
 * Receive a single byte (blocking).
 */
static inline uint8_t saturn_uart_getc(const saturn_uart16550_t* uart) {
    while (!saturn_uart_rx_ready(uart));
    return saturn_uart_reg_read(uart, SATURN_UART_RBR);
}

/**
 * Receive a byte with timeout.
 * @return received byte (0-255), or -1 on timeout
 */
static inline int saturn_uart_getc_timeout(const saturn_uart16550_t* uart,
                                            uint32_t timeout) {
    while (timeout--) {
        if (saturn_uart_rx_ready(uart)) {
            return saturn_uart_reg_read(uart, SATURN_UART_RBR);
        }
    }
    return -1;
}

/**
 * Send a null-terminated string.
 * @return true if all sent, false on timeout
 */
static inline bool saturn_uart_puts(const saturn_uart16550_t* uart,
                                     const char* str) {
    while (*str) {
        if (!saturn_uart_putc(uart, *str++)) return false;
    }
    return true;
}

/**
 * Read raw bytes into buffer. Resets inter-byte timeout on each byte.
 * @return number of bytes read (0 if nothing received before timeout)
 */
static inline int saturn_uart_read_raw(const saturn_uart16550_t* uart,
                                        char* buf, int max_len,
                                        uint32_t timeout) {
    int idx = 0;
    uint32_t remaining = timeout;

    while (idx < max_len && remaining > 0) {
        if (saturn_uart_rx_ready(uart)) {
            if (saturn_uart_rx_error(uart)) {
                saturn_uart_clear_errors(uart);
            }
            buf[idx++] = (char)saturn_uart_reg_read(uart, SATURN_UART_RBR);
            remaining = timeout;  /* reset inter-byte timeout */
        } else {
            remaining--;
        }
    }
    return idx;
}

/**
 * Flush any pending received data.
 */
static inline void saturn_uart_flush_rx(const saturn_uart16550_t* uart) {
    while (saturn_uart_rx_ready(uart)) {
        (void)saturn_uart_reg_read(uart, SATURN_UART_RBR);
    }
}

/* =========================================================================
 * Loopback Self-Test
 * ========================================================================= */

/**
 * Test UART data path using 16550 built-in loopback mode (MCR bit 4).
 * Sends a test byte through the internal loopback and verifies readback.
 * This confirms the TX/RX data path works independent of the modem.
 *
 * @return true if loopback byte matches, false otherwise
 */
static inline bool saturn_uart_loopback_test(const saturn_uart16550_t* uart) {
    uint8_t old_mcr = saturn_uart_reg_read(uart, SATURN_UART_MCR);

    /* Enable loopback mode with DTR + RTS */
    saturn_uart_reg_write(uart, SATURN_UART_MCR,
        SATURN_UART_MCR_LOOP | SATURN_UART_MCR_DTR | SATURN_UART_MCR_RTS);

    /* Flush any pending RX data */
    saturn_uart_flush_rx(uart);

    /* Send test byte */
    saturn_uart_reg_write(uart, SATURN_UART_THR, 0x55);

    /* Wait for loopback to complete */
    for (volatile int i = 0; i < 10000; i++);

    /* Check readback */
    bool ok = saturn_uart_rx_ready(uart) &&
              (saturn_uart_reg_read(uart, SATURN_UART_RBR) == 0x55);

    /* Restore original MCR */
    saturn_uart_reg_write(uart, SATURN_UART_MCR, old_mcr);

    return ok;
}

#endif /* SATURN_UART16550_H */
