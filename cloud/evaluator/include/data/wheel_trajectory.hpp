#ifndef WHEEL_TRAJECTORY_HPP
#define WHEEL_TRAJECTORY_HPP

#include <cstdint>
#include <vector>

namespace uroad_m100 {

struct TraTime {
    uint32_t sec{0};
    uint32_t nsec{0};
};

struct WheelPoint {
    double x{0.0};
    double y{0.0};
    double z{-1.0};
    float  clz{26.0f};
};

struct WheelTrajectory {
    TraTime                 time;
    int32_t                 pts_num{0};  // eval-only: 后段轨迹点数
    std::vector<WheelPoint> frontLeftTrajectory;
    std::vector<WheelPoint> frontRightTrajectory;
    std::vector<WheelPoint> rearLeftTrajectory;
    std::vector<WheelPoint> rearRightTrajectory;
};

}  // namespace uroad_m100

#endif  // WHEEL_TRAJECTORY_HPP
