warning('off', 'MATLAB:imagesci:png:libraryWarning');
clc; clear; close all;
rng(7);

FRAMES_DIR = 'mug_dataset';

calib = jsondecode(fileread('camera_intrinsics.json'));
imu_data = jsondecode(fileread('imu_data.json'));

K = [calib.fx, 0, calib.cx;
     0, calib.fy, calib.cy;
     0, 0, 1];
img_w = calib.width;
img_h = calib.height;
num_frames = numel(imu_data);

mapper = LiveVisualInertialMapper(K);

fig = figure('Name', 'Live Visual-Inertial Gaussian Splatting', ...
    'Color', [0.1 0.1 0.12], 'Position', [100, 100, 1200, 600]);

ax1 = subplot(1, 2, 1);
img_handle = imshow(zeros(img_h, img_w, 3, 'uint8'), 'Parent', ax1);
title(ax1, 'Incoming Frame', 'Color', 'w', 'FontSize', 14);

ax2 = subplot(1, 2, 2);
hold(ax2, 'on'); grid(ax2, 'on'); axis(ax2, 'equal'); view(ax2, 3);
title(ax2, 'Live Map (Frames + IMU)', 'Color', 'w', 'FontSize', 14);
set(ax2, 'Color', 'none', 'XColor', 'w', 'YColor', 'w', 'ZColor', 'w');

drone_marker = scatter3(ax2, 0, 0, 0, 120, 'r', 'v', 'filled', ...
    'MarkerEdgeColor', 'w', 'LineWidth', 1.5);
trail = plot3(ax2, 0, 0, 0, 'r--', 'LineWidth', 1);
scene_light = camlight(ax2, 'headlight');
active_scatter = scatter3(ax2, [], [], [], 4, [], 'filled', 'MarkerFaceAlpha', 0.9);

fprintf('Starting automatic animation for %d frames...\n', num_frames);

prev_timestamp = [];

for k = 1:num_frames
    if ~isvalid(fig)
        break;
    end

    img_name = fullfile(FRAMES_DIR, sprintf('frame_%04d.png', k));
    if ~isfile(img_name)
        fprintf('  skipping: %s not found\n', img_name);
        continue;
    end
    img = imread(img_name);

    if isempty(prev_timestamp)
        dt = 1.0 / 30.0;   
    else
        dt = imu_data(k).timestamp - prev_timestamp;
    end
    prev_timestamp = imu_data(k).timestamp;

    stats = mapper.addFrame(img, imu_data(k), dt);
    fprintf('Frame [%d/%d] | +%d new, %d re-observed (map: %d pts) | cam: [%.2f %.2f %.2f]\n', ...
        k, num_frames, stats.num_new_points, stats.num_reobserved, stats.total_points, stats.est_cam_pos);

    img_handle.CData = img;
    xlim(ax1, [0.5, size(img, 2) + 0.5]);
    ylim(ax1, [0.5, size(img, 1) + 0.5]);

    if ~isempty(mapper.points)
        active_scatter.XData = mapper.points(:, 1);
        active_scatter.YData = mapper.points(:, 2);
        active_scatter.ZData = mapper.points(:, 3);
        active_scatter.CData = mapper.colors / 255.0;
    end

    drone_marker.XData = stats.est_cam_pos(1);
    drone_marker.YData = stats.est_cam_pos(2);
    drone_marker.ZData = stats.est_cam_pos(3);
    trail.XData = mapper.cam_trail(:, 1);
    trail.YData = mapper.cam_trail(:, 2);
    trail.ZData = mapper.cam_trail(:, 3);

    all_x = [mapper.cam_trail(:, 1); mapper.points(:, 1)];
    all_y = [mapper.cam_trail(:, 2); mapper.points(:, 2)];
    all_z = [mapper.cam_trail(:, 3); mapper.points(:, 3)];

    margin = 1.0;
    xlim(ax2, [min(all_x) - margin, max(all_x) + margin]);
    ylim(ax2, [min(all_y) - margin, max(all_y) + margin]);
    zlim(ax2, [min(all_z) - margin, max(all_z) + margin]);

    view(ax2, 45 + k * 0.8, 25);
    camlight(scene_light, 'headlight');
    
    drawnow limitrate;

    if ~isempty(mapper.points)
        mapper.exportPLY('live_map_latest.ply');
    end
end

fprintf('Animation finished. Exporting final reconstruction...\n');
if ~isempty(mapper.points)
    mapper.exportPLY('reconstructed_house_gs.ply');
else
    fprintf('No points triangulated.\n');
end