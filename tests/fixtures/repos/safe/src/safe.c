#include <stddef.h>
#include <stdint.h>

/* Minimal target: a byte-sum parser with no memory-safety issues. */
int safe_parse(const uint8_t *data, size_t size) {
    size_t i;
    int sum = 0;
    for (i = 0; i < size; i++) {
        sum += data[i];
    }
    return sum;
}
