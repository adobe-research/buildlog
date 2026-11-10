#include <iostream>
#include <zlib.h>

int greeting();

int main() {
    std::cout << "hello " << greeting() << " " << zlibVersion() << "\n";
    return 0;
}
