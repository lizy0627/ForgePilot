cmake_minimum_required(VERSION 3.21)
project(CounterFixture LANGUAGES CXX)
add_executable(counter main.cpp App.cpp Counter.cpp)
target_compile_features(counter PRIVATE cxx_std_20)
