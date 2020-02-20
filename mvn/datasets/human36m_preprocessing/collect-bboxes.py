"""
    Read bbox *.mat files from Human3.6M and convert them to a single *.npy file.
    Example of an original bbox file:
    <path-to-Human3.6M-root>/S1/MySegmentsMat/ground_truth_bb/WalkingDog 1.54138969.mat

    Usage:
    python3 collect-bboxes.py <path-to-Human3.6M-root> <num-processes>
"""
import os
import sys
import numpy as np
import h5py
# Some bbox files do not exist, can be misaligned, damaged etc.
from action_to_bbox_filename import action_to_bbox_filename
from collections import defaultdict

dataset_path = sys.argv[1]
subjects = os.listdir(dataset_path)
assert all(subject.startswith('S') for subject in subjects)

nesteddict = lambda: defaultdict(nesteddict)

bboxes_retval = nesteddict()


def load_bboxes(dataset_path, subject, action, camera):
    print(subject, action, camera)

    def mask_to_bbox(mask):
        h_mask = mask.max(0)
        w_mask = mask.max(1)

        top = h_mask.argmax()
        bottom = len(h_mask) - h_mask[::-1].argmax()

        left = w_mask.argmax()
        right = len(w_mask) - w_mask[::-1].argmax()

        return top, left, bottom, right

    try:
        try:
            corrected_action = action_to_bbox_filename[subject][action]
        except KeyError:
            corrected_action = action.replace('-', ' ')

        # TODO use pathlib
        bboxes_path = os.path.join(
            dataset_path,
            subject,
            'MySegmentsMat',
            'ground_truth_bb',
            '%s.%s.mat' % (corrected_action, camera))

        with h5py.File(bboxes_path, 'r') as h5file:
            retval = np.empty((len(h5file['Masks']), 4), dtype=np.int32)

            for frame_idx, mask_reference in enumerate(h5file['Masks'][:,0]):
                bbox_mask = np.array(h5file[mask_reference])
                retval[frame_idx] = mask_to_bbox(bbox_mask)
                top, left, bottom, right = retval[frame_idx]
                if right-left < 2 or bottom-top < 2:
                    raise Exception(str(bboxes_path) + ' $ ' + str(frame_idx))
    except Exception as ex:
        # reraise with path information
        raise Exception(str(ex) + '; %s %s %s' % (subject, action, camera))

    return retval, subject, action, camera


# retval['S1']['Talking-1']['54534623'].shape = (n_frames, 4) # top, left, bottom, right
def add_result_to_retval(args):
    bboxes, subject, action, camera = args
    bboxes_retval[subject][action][camera] = bboxes


import multiprocessing
num_processes = int(sys.argv[2])
pool = multiprocessing.Pool(num_processes)
async_errors = []

for subject in subjects:
    subject_path = os.path.join(dataset_path, subject)
    actions = os.listdir(subject_path)
    try:
        actions.remove('MySegmentsMat')  # folder with bbox *.mat files
    except ValueError:
        pass

    for action in actions:
        cameras = '54138969', '55011271', '58860488', '60457274'

        for camera in cameras:
            async_result = pool.apply_async(
                load_bboxes,
                args=(dataset_path, subject, action, camera),
                callback=add_result_to_retval)
            async_errors.append(async_result)

pool.close()
pool.join()

# raise any exceptions from pool's processes
for async_result in async_errors:
    async_result.get()


def freeze_defaultdict(x):
    x.default_factory = None
    for value in x.values():
        if type(value) is defaultdict:
            freeze_defaultdict(value)


# convert to normal dict
freeze_defaultdict(bboxes_retval)
np.save('bboxes-Human36M-GT.npy', bboxes_retval)
