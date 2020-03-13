import os
from collections import defaultdict
import pickle

import numpy as np
import cv2

import torch
from torch.utils.data import Dataset

from mvn.utils.multiview import Camera
from mvn.utils.img import get_square_bbox, resize_image, crop_image, normalize_image, scale_bbox
from mvn.utils import volumetric


class MultiViewDataset(Dataset):
    """
        Human3.6M for multiview tasks.
    """
    def __init__(self,
                 data_root='/Vol1/dbstore/datasets/Human3.6M/processed/',
                 labels_path='/Vol1/dbstore/datasets/Human3.6M/extra/human36m-multiview-labels-SSDbboxes.npy',
                 pred_results_path=None,
                 image_shape=(256, 256),
                 train=False,
                 test=False,
                 retain_every_n_frames_in_test=1,
                 with_damaged_actions=False,
                 cuboid_side=2000.0,
                 scale_bbox=1.5,
                 norm_image=True,
                 kind="mpii",
                 undistort_images=False,
                 ignore_cameras=[],
                 crop=True
                 ):
        """
            h36m_root:
                Path to 'processed/' directory in Human3.6M
            labels_path:
                Path to 'human36m-multiview-labels.npy' generated by 'generate-labels-npy-multiview.py'
                from https://github.sec.samsung.net/RRU8-VIOLET/human36m-preprocessing
            retain_every_n_frames_test:
                By default, there are 159 181 frames in training set and 26 634 in test (val) set.
                With this parameter, test set frames will be evenly skipped frames so that the
                test set size is `26634 // retain_every_n_frames_test`.
                Use a value of 13 to get 2049 frames in test set.
            with_damaged_actions:
                If `True`, will include 'S9/[Greeting-2,SittingDown-2,Waiting-1]' in test set.
            kind:
                Keypoint format, 'mpii' or 'human36m'
            ignore_cameras:
                A list with indices of cameras to exclude (0 to 3 inclusive)
        """
        assert train or test, '`Human36MMultiViewDataset` must be constructed with at least ' \
                              'one of `test=True` / `train=True`'
        assert kind in ("mpii", "human36m", "humaneva", "ama", "totalcap", "mpi3d")

        self.data_root = data_root
        self.labels_path = labels_path
        self.image_shape = None if image_shape is None else tuple(image_shape)
        self.scale_bbox = scale_bbox
        self.norm_image = norm_image
        self.cuboid_side = cuboid_side
        self.kind = kind
        self.undistort_images = undistort_images
        self.ignore_cameras = ignore_cameras
        self.crop = crop

        self.labels = np.load(labels_path, allow_pickle=True).item()

        n_cameras = len(self.labels['camera_names'])
        assert all(camera_idx in range(n_cameras) for camera_idx in self.ignore_cameras)

        if kind != "ama":
            if kind == "human36m":
                train_subjects = ['S1', 'S5', 'S6', 'S7', 'S8']
                test_subjects = ['S9', 'S11']
                train_actions = ['Directions-1', 'Directions-2',
                                 'Discussion-1', 'Discussion-2',
                                 'Eating-1', 'Eating-2',
                                 'Greeting-1', 'Greeting-2',
                                 'Phoning-1', 'Phoning-2',
                                 'Posing-1', 'Posing-2',
                                 'Purchases-1', 'Purchases-2',
                                 'Smoking-1', 'Smoking-2',
                                 'TakingPhoto-1', 'TakingPhoto-2',
                                 'Waiting-1', 'Waiting-2',
                                 'Walking-1', 'Walking-2',
                                 'WalkingDog-1', 'WalkingDog-2',
                                 'WalkingTogether-1', 'WalkingTogether-2']
                test_actions = ['Sitting-1', 'Sitting-2',
                                'SittingDown-1', 'SittingDown-2']
            elif kind == "humaneva":
                train_subjects = []
                # test_subjects = ['S1', 'S2', 'S3']
                test_subjects = ['S1']
            elif kind == "totalcap":
                train_subjects = []
                test_subjects = ['s1']
            elif kind == "mpi3d":
                train_subjects = []
                test_subjects = ['S1', 'S2']

            train_subjects = list(self.labels['subject_names'].index(x) for x in train_subjects)
            test_subjects  = list(self.labels['subject_names'].index(x) for x in test_subjects)
            # train_actions = list(self.labels['action_names'].index(x) for x in train_actions)
            # test_actions = list(self.labels['action_names'].index(x) for x in test_actions)

            indices = []
            if train:
                mask = np.isin(self.labels['table']['subject_idx'], train_subjects, assume_unique=True)
                # mask = np.isin(self.labels['table']['action_idx'], train_actions, assume_unique=True)
                indices.append(np.nonzero(mask)[0][::4000])  # skip some frames for debug
            if test:
                mask = np.isin(self.labels['table']['subject_idx'], test_subjects, assume_unique=True)
                # mask = np.isin(self.labels['table']['action_idx'], test_actions, assume_unique=True)

                if not with_damaged_actions and kind == "human36m":
                    mask_S9 = self.labels['table']['subject_idx'] == self.labels['subject_names'].index('S9')

                    damaged_actions = 'Greeting-2', 'SittingDown-2', 'Waiting-1'
                    damaged_actions = [self.labels['action_names'].index(x) for x in damaged_actions]
                    mask_damaged_actions = np.isin(self.labels['table']['action_idx'], damaged_actions)

                    mask &= ~(mask_S9 & mask_damaged_actions)
                # if not with_damaged_actions and kind == "human36m":
                #     mask_S9 = self.labels['table']['subject_idx'] == self.labels['subject_names'].index('S9')

                #     damaged_actions = 'Greeting-2', 'SittingDown-2', 'Waiting-1'
                #     damaged_actions = [self.labels['action_names'].index(x) for x in damaged_actions]
                #     mask_damaged_actions = np.isin(self.labels['table']['action_idx'], damaged_actions)

                #     mask &= ~(mask_S9 & mask_damaged_actions)

                indices.append(np.nonzero(mask)[0][::retain_every_n_frames_in_test])

            self.labels['table'] = self.labels['table'][np.concatenate(indices)]

        if kind == "mpii":
            self.num_keypoints = 16
        elif kind == "humaneva":
            self.num_keypoints = 20
        elif kind == "mpi3d":
            self.num_keypoints = 28
        elif kind == "totalcap":
            self.num_keypoints = 21
        else:
            self.num_keypoints = 17
        # self.num_keypoints = 16 if kind == "mpii" else 17
        # assert self.labels['table']['keypoints'].shape[1] == 17, "Use a newer 'labels' file"

        self.keypoints_3d_pred = None
        if pred_results_path is not None:
            pred_results = np.load(pred_results_path, allow_pickle=True)
            keypoints_3d_pred = pred_results['keypoints_3d'][np.argsort(pred_results['indexes'])]
            self.keypoints_3d_pred = keypoints_3d_pred[::retain_every_n_frames_in_test]
            print(len(self.keypoints_3d_pred), len(self))
            assert len(self.keypoints_3d_pred) == len(self)

    def __len__(self):
        return len(self.labels['table'])

    def __getitem__(self, idx):
        sample = defaultdict(list)  # return value
        shot = self.labels['table'][idx]

        if self.kind != 'ama':
            subject = self.labels['subject_names'][shot['subject_idx']]
        action = self.labels['action_names'][shot['action_idx']]
        frame_idx = shot['frame_idx']

        for camera_idx, camera_name in enumerate(self.labels['camera_names']):
            if camera_idx in self.ignore_cameras:
                continue
            if self.kind == 'ama':
                if 'march' in action or 'squat' in action:
                    if camera_name == '7':
                        continue
                else:
                    if camera_name == '5':
                        continue

            # load bounding box
            bbox = shot['bbox_by_camera_tlbr'][camera_idx][[1, 0, 3, 2]]  # TLBR to LTRB
            bbox_height = bbox[2] - bbox[0]
            if bbox_height == 0:
                # convention: if the bbox is empty, then this view is missing
                continue

            # scale the bounding box
            bbox = scale_bbox(bbox, self.scale_bbox)

            # load image
            if self.kind in ["human36m" or "mpii"]:
                image_path = os.path.join(self.data_root, subject, action, 'imageSequence' + '-undistorted' * self.undistort_images, camera_name, f'img_{frame_idx+1:06d}.jpg')
            elif self.kind == 'ama':
                if action in ['D_march', 'D_squat', 'I_march', 'I_squat']:
                    image_path = os.path.join(self.data_root, action, 'images', f'Camera{camera_name}_{frame_idx:04d}.jpg')
                else:
                    image_path = os.path.join(self.data_root, action, 'images', f'Image{camera_name}_{frame_idx:04d}.png')
            elif self.kind == "totalcap":
                image_path = os.path.join(self.data_root, subject, 'Images', action, f'cam{camera_name}', f'frm{frame_idx:04d}_cam{camera_name}.jpg')
            elif self.kind == "mpi3d":
                image_path = os.path.join(self.data_root, subject, action, 'Images', f'cam{camera_name}', f'frm{frame_idx:06d}_cam{camera_name}.jpg')
            else:
                image_path = os.path.join(self.data_root, subject, 'imageSequence', action, camera_name, f'img_{frame_idx:06d}.jpg')
            assert os.path.isfile(image_path), '%s doesn\'t exist' % image_path
            image = cv2.imread(image_path)

            # load camera
            if self.kind == 'ama':
                shot_camera = self.labels['cameras'][shot['action_idx'], camera_idx]
            else:
                shot_camera = self.labels['cameras'][shot['subject_idx'], camera_idx]
            retval_camera = Camera(shot_camera['R'], shot_camera['t'], shot_camera['K'], shot_camera['dist'], camera_name)

            if self.crop:
                # crop image
                image = crop_image(image, bbox)
                retval_camera.update_after_crop(bbox)

            if self.image_shape is not None:
                # resize
                image_shape_before_resize = image.shape[:2]
                image = resize_image(image, self.image_shape)
                retval_camera.update_after_resize(image_shape_before_resize, self.image_shape)

                sample['image_shapes_before_resize'].append(image_shape_before_resize)

            if self.norm_image:
                image = normalize_image(image)

            sample['images'].append(image)
            # sample['detections'].append(bbox + (1.0,))  # TODO add real confidences
            sample['detections'].append(bbox)  # TODO add real confidences
            sample['cameras'].append(retval_camera)
            sample['proj_matrices'].append(retval_camera.projection)

        # 3D keypoints
        # add dummy confidences
        sample['keypoints_3d'] = np.pad(
            shot['keypoints'][:self.num_keypoints],
            ((0, 0), (0, 1)), 'constant', constant_values=1.0)

        # build cuboid
        if self.kind == "human36m":
            base_point = sample['keypoints_3d'][6, :3]
            sides = np.array([self.cuboid_side, self.cuboid_side, self.cuboid_side])
            position = base_point - sides / 2
            sample['cuboids'] = volumetric.Cuboid3D(position, sides)

        # save sample's index
        sample['indexes'] = idx
        if self.kind != 'ama':
            sample['subject'] = subject
        sample['action'] = action
        sample['frame_idx'] = frame_idx

        if self.keypoints_3d_pred is not None:
            sample['pred_keypoints_3d'] = self.keypoints_3d_pred[idx]

        sample.default_factory = None
        return sample

    def evaluate_using_per_pose_error(self, per_pose_error, split_by_subject):
        def evaluate_by_actions(self, per_pose_error, mask=None):
            if mask is None:
                mask = np.ones_like(per_pose_error, dtype=bool)

            action_scores = {
                'Average': {'total_loss': per_pose_error[mask].sum(), 'frame_count': np.count_nonzero(mask)}
            }

            for action_idx in range(len(self.labels['action_names'])):
                action_mask = (self.labels['table']['action_idx'] == action_idx) & mask
                action_per_pose_error = per_pose_error[action_mask]
                action_scores[self.labels['action_names'][action_idx]] = {
                    'total_loss': action_per_pose_error.sum(), 'frame_count': len(action_per_pose_error)
                }

            action_names_without_trials = \
                [name[:-2] for name in self.labels['action_names'] if name.endswith('-1')]

            for action_name_without_trial in action_names_without_trials:
                combined_score = {'total_loss': 0.0, 'frame_count': 0}

                for trial in 1, 2:
                    action_name = '%s-%d' % (action_name_without_trial, trial)
                    combined_score['total_loss' ] += action_scores[action_name]['total_loss']
                    combined_score['frame_count'] += action_scores[action_name]['frame_count']
                    del action_scores[action_name]

                action_scores[action_name_without_trial] = combined_score

            for k, v in action_scores.items():
                action_scores[k] = v['total_loss'] / v['frame_count']

            return action_scores

        subject_scores = {
            'Average': evaluate_by_actions(self, per_pose_error)
        }

        for subject_idx in range(len(self.labels['subject_names'])):
            subject_mask = self.labels['table']['subject_idx'] == subject_idx
            subject_scores[self.labels['subject_names'][subject_idx]] = \
                evaluate_by_actions(self, per_pose_error, subject_mask)

        return subject_scores

    def evaluate(self, keypoints_3d_predicted, idx, split_by_subject=False, transfer_cmu_to_human36m=False, transfer_human36m_to_human36m=False):
        keypoints_gt = self.labels['table']['keypoints'][:, :self.num_keypoints]
        # if keypoints_3d_predicted.shape != keypoints_gt.shape:
        #     raise ValueError(
        #         '`keypoints_3d_predicted` shape should be %s, got %s' % \
        #         (keypoints_gt.shape, keypoints_3d_predicted.shape))

        if transfer_cmu_to_human36m or transfer_human36m_to_human36m:
            human36m_joints = [10, 11, 15, 14, 1, 4]
            if transfer_human36m_to_human36m:
                cmu_joints = [10, 11, 15, 14, 1, 4]
            else:
                cmu_joints = [10, 8, 9, 7, 14, 13]

            keypoints_gt = keypoints_gt[:, human36m_joints]
            keypoints_3d_predicted = keypoints_3d_predicted[:, cmu_joints]

        if self.kind == 'humaneva':
            print('evaluate HumanEva dataset')
            # # L/R knee, L/R elbow, L/R wrist, L/R shoulder, L/R ankle
            # h36m_eval_idx = [4, 1, 14, 11, 15, 10, 13, 12, 5, 0]
            # heva_eval_idx = [11, 15, 3, 7, 5, 9, 2, 6, 13, 17]
            # L/R knee, L/R shoulder, L/R ankle
            # h36m_eval_idx = [4, 1, 13, 12, 5, 0]
            # heva_eval_idx = [11, 15, 2, 6, 13, 17]
            # L/R knee, L/R shoulder, L/R ankle, pelvis, neck
            h36m_eval_idx = [ 4,  1, 13, 12,  5,  0, 6, 8]
            heva_eval_idx = [11, 15,  2,  6, 13, 17, 1, 0]
            print(h36m_eval_idx)
            print(heva_eval_idx)
            keypoints_3d_predicted_tmp = keypoints_3d_predicted[:, h36m_eval_idx]
            keypoints_gt_tmp = keypoints_gt[:, heva_eval_idx]
        else:
            keypoints_3d_predicted_tmp = keypoints_3d_predicted
            keypoints_gt_tmp = keypoints_gt

        # mean error per 16/17 joints in mm, for each pose
        per_pose_error = np.sqrt(((keypoints_gt_tmp[idx] - keypoints_3d_predicted_tmp) ** 2).sum(2)).mean(1)

        # relative mean error per 16/17 joints in mm, for each pose
        if not (transfer_cmu_to_human36m or transfer_human36m_to_human36m):
            root_index = 6 if self.kind == "mpii" else 6
        else:
            root_index = 0

        keypoints_gt_relative = keypoints_gt - keypoints_gt[:, root_index:root_index + 1, :]
        keypoints_3d_predicted_relative = keypoints_3d_predicted - keypoints_3d_predicted[:, root_index:root_index + 1, :]

        if self.kind == 'humaneva':
            per_pose_error_relative = np.sqrt(((keypoints_gt_relative[:, heva_eval_idx] - keypoints_3d_predicted_relative[:, h36m_eval_idx]) ** 2).sum(2)).mean(1)
        else:
            per_pose_error_relative = np.sqrt(((keypoints_gt_relative - keypoints_3d_predicted_relative) ** 2).sum(2)).mean(1)

        result = {
            'per_pose_error': self.evaluate_using_per_pose_error(per_pose_error, split_by_subject),
            'per_pose_error_relative': self.evaluate_using_per_pose_error(per_pose_error_relative, split_by_subject)
        }

        return result['per_pose_error_relative']['Average']['Average'], result
