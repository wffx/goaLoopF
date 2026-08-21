#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Intentionally fragile target: any input of size >= 2 overflows a 1-byte
 * stack buffer, which ASan catches deterministically. */
int fragile_parse(const uint8_t *data, size_t size) {
    char buffer[1];
    if (size > 0) {
        memcpy(buffer, data, size);
        return buffer[0];
    }
    return 0;
}
