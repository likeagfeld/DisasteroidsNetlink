/**
 * net_transport.h - Transport Abstraction Layer for Disasteroids
 *
 * Byte-stream transport interface for networked play.
 * Saturn implementation: UART over NetLink modem.
 *
 * Adapted from cui_transport.h in the CUI Platform Abstraction Layer.
 */

#ifndef NET_TRANSPORT_H
#define NET_TRANSPORT_H

#include <stdint.h>
#include <stdbool.h>

typedef struct net_transport {
    /**
     * Check if at least one byte is available to read.
     * Non-blocking.
     */
    bool (*rx_ready)(void* ctx);

    /**
     * Read a single byte. Only call when rx_ready() returns true.
     */
    uint8_t (*rx_byte)(void* ctx);

    /**
     * Send a buffer of bytes.
     * @return number of bytes sent, or -1 on error
     */
    int (*send)(void* ctx, const uint8_t* data, int len);

    /**
     * Check if the transport link is still active.
     * May be NULL if the transport has no notion of connection state.
     */
    bool (*is_connected)(void* ctx);

    /** Opaque context pointer passed to all callbacks. */
    void* ctx;

} net_transport_t;

/*============================================================================
 * Convenience Helpers
 *============================================================================*/

static inline bool net_transport_rx_ready(const net_transport_t* t)
{
    return (t && t->rx_ready) ? t->rx_ready(t->ctx) : false;
}

static inline uint8_t net_transport_rx_byte(const net_transport_t* t)
{
    return (t && t->rx_byte) ? t->rx_byte(t->ctx) : 0;
}

static inline int net_transport_send(const net_transport_t* t,
                                     const uint8_t* data, int len)
{
    return (t && t->send) ? t->send(t->ctx, data, len) : -1;
}

static inline bool net_transport_is_connected(const net_transport_t* t)
{
    if (!t) return false;
    return t->is_connected ? t->is_connected(t->ctx) : true;
}

#endif /* NET_TRANSPORT_H */
