#include "sandbox/fd_owner.hpp"

#include <cassert>
#include <cstdio>
#include <utility>

int main() {
    sandbox::FdOwner a(7);
    assert(a.valid());
    assert(a.get() == 7);

    sandbox::FdOwner b(std::move(a));
    assert(b.get() == 7);
    assert(!a.valid() && "moved-from owner must relinquish the descriptor");

    sandbox::FdOwner c;
    c = std::move(b);
    assert(c.get() == 7);
    assert(!b.valid());

    assert(c.release() == 7);
    assert(!c.valid());

    std::puts("fd_owner ok");
    return 0;
}
