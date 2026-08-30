classdef LiveVisualInertialMapper < handle
    properties
        K
        ekf

        prev_gray = [];
        prev_pts = [];       
        prev_feats = [];     
        prev_R = [];
        prev_p = [];

        points = zeros(0, 3);
        colors = zeros(0, 3);
        obs_count = zeros(0, 1);
        opacities = zeros(0, 1);
        scales = zeros(0, 3);
        rots = zeros(0, 4);

        frame_idx = 0;
        cam_trail = zeros(0, 3);
    end

    properties (Constant)
        SH_C0 = 0.28209479177387814;
        BASE_SCALE = 0.02;
        MIN_SCALE  = 0.004;
        BASE_OPACITY = 2.0;
        MAX_OPACITY  = 6.0;

        MIN_MATCHES = 8;        
        MIN_BASELINE = 0.02;    
        MAX_REPROJ_DEPTH = 20;  
        REOBSERVE_RADIUS_PX = 3; 
    end

    methods
        function obj = LiveVisualInertialMapper(K)
            obj.K = K;
            obj.ekf = InertialNavEKF();
        end

        function stats = addFrame(obj, img, imu_sample, dt)
            obj.frame_idx = obj.frame_idx + 1;

            if ~obj.ekf.attitude_initialized
                obj.ekf.initializeAttitude(imu_sample.accel_body_xyz);
            end
            obj.ekf.predict(imu_sample.gyro_body_xyz, imu_sample.accel_body_xyz, dt);

            curr_R = obj.ekf.R_wb;
            curr_p = obj.ekf.p;
            obj.cam_trail(end + 1, :) = curr_p';

            gray = rgb2gray(img);
            orb_pts = detectORBFeatures(gray);
            [curr_feats, curr_valid_pts] = extractFeatures(gray, orb_pts);
            curr_pts = curr_valid_pts.Location;  

            num_new_points = 0;
            num_reobserved = 0;

            if ~isempty(obj.prev_feats) && ~isempty(curr_feats)
                idxPairs = matchFeatures(obj.prev_feats, curr_feats, ...
                    'Unique', true, 'MaxRatio', 0.8);

                baseline = norm(curr_p - obj.prev_p);

                if size(idxPairs, 1) >= obj.MIN_MATCHES && baseline >= obj.MIN_BASELINE
                    matchedPrev = obj.prev_pts(idxPairs(:, 1), :);
                    matchedCurr = curr_pts(idxPairs(:, 2), :);

                    P1 = build_projection_matrix(obj.K, obj.prev_R, obj.prev_p);
                    P2 = build_projection_matrix(obj.K, curr_R, curr_p);

                    for i = 1:size(matchedPrev, 1)
                        X = triangulate_point(P1, P2, matchedPrev(i, :), matchedCurr(i, :));

                        depth1 = cam_depth(X, obj.prev_R, obj.prev_p);
                        depth2 = cam_depth(X, curr_R, curr_p);
                        if depth1 <= 0 || depth2 <= 0
                            continue;   
                        end
                        if depth1 > obj.MAX_REPROJ_DEPTH || depth2 > obj.MAX_REPROJ_DEPTH
                            continue;   
                        end

                        px = round(matchedCurr(i, 1));
                        py = round(matchedCurr(i, 2));
                        if px < 1 || px > size(img, 2) || py < 1 || py > size(img, 1)
                            continue;
                        end
                        color = double(squeeze(img(py, px, :)))';

                        obj.points(end + 1, :) = X;
                        obj.colors(end + 1, :) = color;
                        obj.obs_count(end + 1, 1) = 1;
                        obj.opacities(end + 1, 1) = obj.BASE_OPACITY;
                        obj.scales(end + 1, :) = log(obj.BASE_SCALE) * ones(1, 3);
                        obj.rots(end + 1, :) = [1 0 0 0];
                        num_new_points = num_new_points + 1;
                    end
                end
            end

            valid_3D = zeros(0, 3);
            valid_2D = zeros(0, 2);

            if ~isempty(obj.points) && ~isempty(curr_pts)
                p_cam = (obj.points - curr_p') * curr_R;
                depth = -p_cam(:, 3);
                fx = obj.K(1,1); fy = obj.K(2,2); cx = obj.K(1,3); cy = obj.K(2,3);
                u = cx + fx * (p_cam(:, 1) ./ depth);
                v = cy - fy * (p_cam(:, 2) ./ depth);
                in_front = depth > 0;

                for i = find(in_front)'
                    d_sq = (curr_pts(:, 1) - u(i)).^2 + (curr_pts(:, 2) - v(i)).^2;
                    [min_d, match_idx] = min(d_sq);
                    if min_d <= obj.REOBSERVE_RADIUS_PX^2
                        px = round(curr_pts(match_idx, 1)); 
                        py = round(curr_pts(match_idx, 2));
                        if px >= 1 && px <= size(img, 2) && py >= 1 && py <= size(img, 1)
                            n = obj.obs_count(i);
                            newc = double(squeeze(img(py, px, :)))';
                            obj.colors(i, :) = (obj.colors(i, :) .* n + newc) ./ (n + 1);
                            obj.obs_count(i) = n + 1;
                            conf = obj.obs_count(i);
                            obj.scales(i, :) = log(max(obj.MIN_SCALE, obj.BASE_SCALE / sqrt(conf))) * ones(1,3);
                            obj.opacities(i) = min(obj.MAX_OPACITY, obj.BASE_OPACITY + 0.75 * conf);
                            num_reobserved = num_reobserved + 1;

                            valid_3D(end + 1, :) = obj.points(i, :);
                            valid_2D(end + 1, :) = curr_pts(match_idx, :);
                        end
                    end
                end
            end

            if size(valid_3D, 1) >= 6
                try
                    [img_h, img_w, ~] = size(img);
                    intrinsics = cameraIntrinsics([obj.K(1,1), obj.K(2,2)], [obj.K(1,3), obj.K(2,3)], [img_h, img_w]);
                    [worldPose, inlierIdx] = estworldpose(valid_2D, valid_3D, intrinsics, 'MaxReprojectionError', 3.0, 'Confidence', 99);
                    if sum(inlierIdx) >= 6
                        p_meas = worldPose.Translation(:);
                        obj.ekf.updatePosition(p_meas);
                        curr_p = obj.ekf.p;
                    end
                catch
                end
            end

            obj.prev_gray = gray;
            obj.prev_pts = curr_pts;
            obj.prev_feats = curr_feats;
            obj.prev_R = curr_R;
            obj.prev_p = curr_p;

            stats.frame_idx = obj.frame_idx;
            stats.num_new_points = num_new_points;
            stats.num_reobserved = num_reobserved;
            stats.total_points = size(obj.points, 1);
            stats.est_cam_pos = curr_p';
        end

        function exportPLY(obj, filename)
            n_out = size(obj.points, 1);
            if n_out == 0
                warning('LiveVisualInertialMapper:emptyMap', 'No points to export.');
                return;
            end
            rgb_normalized = obj.colors / 255.0;
            f_dc = (rgb_normalized - 0.5) / obj.SH_C0;
            dummy_normals = zeros(n_out, 3);

            fid = fopen(filename, 'w');
            fprintf(fid, 'ply\nformat ascii 1.0\nelement vertex %d\n', n_out);
            fprintf(fid, 'property float x\nproperty float y\nproperty float z\n');
            fprintf(fid, 'property float nx\nproperty float ny\nproperty float nz\n');
            fprintf(fid, 'property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n');
            fprintf(fid, 'property float opacity\nproperty float scale_0\n');
            fprintf(fid, 'property float scale_1\nproperty float scale_2\n');
            fprintf(fid, 'property float rot_0\nproperty float rot_1\n');
            fprintf(fid, 'property float rot_2\nproperty float rot_3\nend_header\n');

            gs_data = [obj.points, dummy_normals, f_dc, obj.opacities, obj.scales, obj.rots];
            fprintf(fid, '%f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f\n', gs_data');
            fclose(fid);
        end
    end
end

function P = build_projection_matrix(K, R, C)
    Rc = R';                        
    S = diag([1, -1, -1]);          
    Rcv = S * Rc;
    tcv = -Rcv * C(:);
    P = K * [Rcv, tcv];
end

function X = triangulate_point(P1, P2, pt1, pt2)
    u1 = pt1(1); v1 = pt1(2);
    u2 = pt2(1); v2 = pt2(2);
    A = [u1 * P1(3, :) - P1(1, :);
         v1 * P1(3, :) - P1(2, :);
         u2 * P2(3, :) - P2(1, :);
         v2 * P2(3, :) - P2(2, :)];
    [~, ~, V] = svd(A);
    Xh = V(:, end);
    X = (Xh(1:3) / Xh(4))';
end

function d = cam_depth(X, R, C)
    p_cam = (X - C') * R;
    d = -p_cam(3);
end