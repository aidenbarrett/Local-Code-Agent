#include <cstdio>

int main() {
    // Bounded work. The timeout scenario replaces the bound with a condition
    // that never becomes false.
    unsigned long long acc = 0;
    for (unsigned long long i = 0; i < 2000000ULL; ++i) {
        acc += i % 7;
    }
    std::printf("slow ok %llu\n", acc);
    return 0;
}
