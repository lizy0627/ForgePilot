add_library(subject STATIC src/subject.cpp src/subject.h)
target_include_directories(subject PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src")
