#include <stddef.h>
#include <stdint.h>
#include "target.h"

int cmake_parse(const uint8_t *data, size_t size) {
    size_t i;
    int sum = 0;
    for (i = 0; i < size; i++) {
        sum += data[i];
    }
    return sum;
}
