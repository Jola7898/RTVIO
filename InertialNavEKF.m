classdef InertialNavEKF < handle
    properties
        R_wb                 
        p                     
        v                     
        ba                    
        P                     
        attitude_initialized = false;

        GRAVITY = 9.81;
        EARTH_RADIUS_M = 6378137.0;

        accel_noise_std = 0.08;      
        bias_walk_std = 0.001;       
        pos_prior_std = 5.0;         
        vel_prior_std = 2.0;         
        bias_prior_std = 0.3;        
    end

    methods
        function obj = InertialNavEKF()
            obj.R_wb = eye(3);
            obj.p = zeros(3, 1);
            obj.v = zeros(3, 1);
            obj.ba = zeros(3, 1);
            obj.P = blkdiag(obj.pos_prior_std^2 * eye(3), ...
                             obj.vel_prior_std^2 * eye(3), ...
                             obj.bias_prior_std^2 * eye(3));
        end

        function initializeState(obj, R_init, p_init, v_init)
            obj.R_wb = R_init;
            obj.p = p_init(:);
            obj.v = v_init(:);
            obj.attitude_initialized = true;
        end

        function initializeAttitude(obj, accel_first_body)
            accel_first_body = accel_first_body(:);
            b = accel_first_body / norm(accel_first_body);   
            w = [0; 0; 1];                                    

            axis = cross(b, w);
            s = norm(axis);
            c = dot(b, w);
            if s < 1e-8
                obj.R_wb = eye(3);   
            else
                axis = axis / s;
                K = [0 -axis(3) axis(2); axis(3) 0 -axis(1); -axis(2) axis(1) 0];
                angle = atan2(s, c);
                obj.R_wb = eye(3) + sin(angle) * K + (1 - cos(angle)) * (K * K);
            end
            obj.attitude_initialized = true;
        end

        function predict(obj, gyro, accel, dt)
            gyro = gyro(:);
            accel = accel(:);

            dtheta = gyro * dt;
            obj.R_wb = obj.R_wb * axang_to_R(dtheta);
            [Uo, ~, Vo] = svd(obj.R_wb);
            obj.R_wb = Uo * Vo';

            a_meas_world = obj.R_wb * accel - [0; 0; obj.GRAVITY];
            a_corrected = a_meas_world - obj.R_wb * obj.ba;

            obj.p = obj.p + obj.v * dt + 0.5 * dt^2 * a_corrected;
            obj.v = obj.v + dt * a_corrected;

            F = eye(9);
            F(1:3, 4:6) = eye(3) * dt;
            F(1:3, 7:9) = -0.5 * dt^2 * obj.R_wb;
            F(4:6, 7:9) = -dt * obj.R_wb;

            Qc = blkdiag(zeros(3), obj.accel_noise_std^2 * eye(3), obj.bias_walk_std^2 * eye(3));
            Q = Qc * dt;   

            obj.P = F * obj.P * F' + Q;
        end

        function updatePosition(obj, p_meas)
            z = p_meas(:) - obj.p;
            H = zeros(3, 9);
            H(1:3, 1:3) = eye(3);
            R_cov = 0.01 * eye(3);
            S = H * obj.P * H' + R_cov;
            K_gain = obj.P * H' / S;
            dx = K_gain * z;
            obj.p = obj.p + dx(1:3);
            obj.v = obj.v + dx(4:6);
            obj.ba = obj.ba + dx(7:9);
            obj.P = (eye(9) - K_gain * H) * obj.P;
        end
    end
end

function R = axang_to_R(dtheta)
    angle = norm(dtheta);
    if angle < 1e-12
        R = eye(3);
        return;
    end
    axis = dtheta / angle;
    K = [0 -axis(3) axis(2); axis(3) 0 -axis(1); -axis(2) axis(1) 0];
    R = eye(3) + sin(angle) * K + (1 - cos(angle)) * (K * K);
end