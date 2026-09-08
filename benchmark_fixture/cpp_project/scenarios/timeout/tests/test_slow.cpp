#include <cstdio>

int main() {
    // The bound is never reached: i wraps and the loop never terminates.
    unsigned char i = 0;
    unsigned long long acc = 0;
    while (i < 300) {
        acc += i;
        ++i;
    }
    std::printf("slow ok %llu\n", acc);
    return 0;
}
