clc; clear; close all;

imu_data = jsondecode(fileread('imu_data.json'));
gt_data = jsondecode(fileread('gt_trajectory.json'));

num_frames = numel(imu_data);
est_trajectory = zeros(num_frames, 3);
gt_trajectory_shifted = zeros(num_frames, 3);

ekf = InertialNavEKF();
prev_timestamp = [];

gt_pos_start = gt_data(1).pos_xyz';

for k = 1:num_frames
    gt_trajectory_shifted(k, :) = gt_data(k).pos_xyz' - gt_pos_start;
    
    if isempty(prev_timestamp)
        dt = 1.0 / 30.0;
    else
        dt = imu_data(k).timestamp - prev_timestamp;
    end
    prev_timestamp = imu_data(k).timestamp;
    
    if ~ekf.attitude_initialized
        q = gt_data(1).rot_xyzw;
        qw = q(1); qx = q(2); qy = q(3); qz = q(4);
        R_init = [1 - 2*qy^2 - 2*qz^2, 2*qx*qy - 2*qz*qw, 2*qx*qz + 2*qy*qw;
                  2*qx*qy + 2*qz*qw, 1 - 2*qx^2 - 2*qz^2, 2*qy*qz - 2*qx*qw;
                  2*qx*qz - 2*qy*qw, 2*qy*qz + 2*qx*qw, 1 - 2*qx^2 - 2*qy^2];
        
        p_init = zeros(3, 1);
        v_init = zeros(3, 1);
        
        ekf.initializeState(R_init, p_init, v_init);
    end
    
    ekf.predict(imu_data(k).gyro_body_xyz, imu_data(k).accel_body_xyz, dt);
    
    est_trajectory(k, :) = ekf.p';
end

fig = figure('Name', 'Pure IMU Trajectory Comparison', 'Color', 'w', 'Position', [100, 100, 900, 600]);
hold on; grid on; axis equal; view(3);

plot3(gt_trajectory_shifted(:, 1), gt_trajectory_shifted(:, 2), gt_trajectory_shifted(:, 3), 'b-', 'LineWidth', 2);
plot3(est_trajectory(:, 1), est_trajectory(:, 2), est_trajectory(:, 3), 'r--', 'LineWidth', 2);

scatter3(gt_trajectory_shifted(1, 1), gt_trajectory_shifted(1, 2), gt_trajectory_shifted(1, 3), 100, 'go', 'filled');
scatter3(gt_trajectory_shifted(end, 1), gt_trajectory_shifted(end, 2), gt_trajectory_shifted(end, 3), 100, 'kx', 'LineWidth', 2);

legend('Ground Truth (Shifted)', 'Pure IMU Estimate', 'Start', 'End', 'Location', 'best');
xlabel('X Axis'); ylabel('Y Axis'); zlabel('Z Axis');
title('Pure IMU Dead Reckoning vs Shifted Ground Truth');