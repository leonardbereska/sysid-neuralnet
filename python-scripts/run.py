import argparse
import json
import copy
import time
import os.path
import train
from logger import set_redirects
import data.loader as loader
from model.model_state import ModelState
from model.base import Normalizer1D
import torch
import numpy as np

default_options_lstm = {
    'hidden_size': 128,
    'ar': True,
    'io_delay': 0,
    'num_layers': 1,
    'dropout': 0
}

default_options_tcn = {
    'ksize': 3,
    'dropout': 0.05,
    'n_channels': [50, 50, 50, 50],
    'dilation_sizes': None,
    'ar': True,
    'io_delay': 0,
    'normalization': 'batch_norm'
}

default_options_mlp = {
    'hidden_size': 8,
    'max_past_input': 4,
    'ar': True,
    'io_delay': 0,
    'activation_fn': 'sigmoid'
}

default_options_chen = {
    'seq_len': 100,
    'train': {
        'ntotbatch': 100,
        'seed': 1,
        'sd_v': 0.3,
        'sd_w': 0.3
    },
    'valid': {
        'ntotbatch': 5,
        'seed': 2,
        'sd_v': 0.3,
        'sd_w': 0.3
    },
    'test': {
        'ntotbatch': 5,
        'seed': 3,
        'sd_v': 0,
        'sd_w': 0
    }
}

default_options_silverbox = {'seq_len_train': 2048,
                             'seq_len_val': 2048,
                             'seq_len_test': None,
                             'train_split': None,
                             'shuffle_seed': None}

default_options_f16gvt = {'seq_len_train': 2048,
                          'seq_len_val': 2048,
                          'seq_len_test': None}

default_options_train = {
        'init_lr': 0.001,
        'min_lr': 1e-6,
        'batch_size': 1,
        'epochs': 10000,
        'lr_scheduler_nepochs': 10,
        'lr_scheduler_factor': 10,
        'log_interval': 1,
        'training_mode': 'one-step-ahead'
}

default_options_optimizer = {
    'optim': 'Adam',
}

default_options_test = {
    'plot': True,
    'plotly': True,
    'batch_size': 10,
}

default_options = {
    'cuda': False,
    'seed': 1111,
    'logdir': None,
    'run_name': None,
    'load_model': None,
    'normalize': False,
    'normalize_n_std': 1,
    'train_options': default_options_train,
    'test_options': default_options_test,
    'optimizer': default_options_optimizer,

    'dataset': "f16gvt",
    'dataset_options': {},
    'chen_options': default_options_chen,
    'silverbox_options': default_options_silverbox,
    'f16gvt_options': default_options_f16gvt,

    'model': 'tcn',
    'model_options': {},
    'tcn_options': default_options_tcn,
    'lstm_options': default_options_lstm,
    'mlp_options': default_options_mlp,

}


def recursive_merge(default_dict, new_dict, path=None, allow_new=False):
    # Stack overflow : https://stackoverflow.com/questions/7204805/dictionaries-of-dictionaries-merge/7205107#7205107

    deprecated_options = ["evaluate_model"]

    if path is None:
        path = []
    for key in new_dict:
        if key in default_dict:
            if isinstance(default_dict[key], dict) and isinstance(new_dict[key], dict):
                if key in ("model_options", "dataset_options"):
                    recursive_merge(default_dict[key], new_dict[key], path + [str(key)], allow_new=True)
                else:
                    recursive_merge(default_dict[key], new_dict[key], path + [str(key)], allow_new=allow_new)
            elif isinstance(default_dict[key], dict) or isinstance(new_dict[key], dict):
                raise Exception('Conflict at %s' % '.'.join(path + [str(key)]))
            else:
                default_dict[key] = new_dict[key]
        else:
            if allow_new:
                default_dict[key] = new_dict[key]
            elif key in deprecated_options:
                print("Key: " + key + " is deprecated")
            else:
                raise Exception('Default value not found at %s' % '.'.join(path + [str(key)]))
    return default_dict


def clean_options(options):
    # Remove unused options
    datasets = ["chen", 'silverbox', 'f16gvt']
    if options["dataset"] not in datasets:
        raise Exception("Unknown dataset: " + options["dataset"])
    dataset_options = options[options["dataset"] + "_options"]

    models = ["tcn", "lstm", "mlp"]
    if options["model"] not in models:
        raise Exception("Unknown model: " + options["model"])
    model_options = options[options["model"] + "_options"]

    remove_options = [name + "_options" for name in datasets + models]
    for key in dict(options):
        if key in remove_options:
            del options[key]

    # Specify used dataset and model options
    options["dataset_options"] = recursive_merge(dataset_options, options["dataset_options"])
    options["model_options"] = recursive_merge(model_options, options["model_options"])
    return options


def get_commandline_args():
    def str2bool(v):
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Boolean value expected.')

    parser = argparse.ArgumentParser()

    parser.add_argument('--logdir', type=str,
                        help='Directory for logs')

    parser.add_argument('--run_name', type=str,
                        help='The name of this run')

    parser.add_argument('--load_model', type=str, help='Path to a saved model')

    parser.add_argument('--evaluate_model', type=str2bool, const=True, nargs='?', help='Evaluate model')

    parser.add_argument('--cuda', type=str2bool, const=True, nargs='?',
                        help='Specify if the model is to be run on the GPU')

    parser.add_argument('--seed', type=int,
                        help='The seed used in pytorch')

    parser.add_argument('--option_file', type=str, default=None,
                        help='File containing Json dict with options (default(%s))')

    parser.add_argument('--option_dict', type=str, default='{}',
                        help='Json with options specified at commandline (default(%s))')

    args = vars(parser.parse_args())

    # Options file
    option_file = args['option_file']

    # Options dict from commandline
    option_dict = json.loads(args['option_dict'])

    commandline_options = {k: v for k, v in args.items() if v is not None and k != "option_file" and k != "option_dict"}

    return commandline_options, option_dict, option_file


def create_full_options_dict(*option_dicts):
    """
    Merges multiple option dictionaries with the default dictionary and an optional option file specifying options in
    Json format.

    :param option_dicts: Any number of option dictionaries either in the form of a dictionary or a file containg Json dict
    :return: A merged option dictionary giving priority in the order of the input
    """

    merged_options = copy.deepcopy(default_options)

    for option_dict in reversed(option_dicts):
        if option_dict is not None:
            if isinstance(option_dict, str):
                with open(option_dict, "r") as file:
                    option_dict = json.loads(file.read())

            merged_options = recursive_merge(merged_options, option_dict)

    # Clear away unused fields and merge model options
    options = clean_options(merged_options)

    return options


def get_run_path(options, ctime):
    logdir = options.get("logdir", None)
    run_name = options.get("run_name", None)

    if run_name is None:
        run_name = "train_" + ctime

    if logdir is None:
        logdir = "log"

    run_path = os.path.join(logdir, run_name)
    return run_path


def compute_normalizers(loader_train, variance_scaler):
    total_batches = 0
    u_mean = 0
    y_mean = 0
    u_var = 0
    y_var = 0
    for i, (u, y) in enumerate(loader_train):
        total_batches += u.size()[0]
        u_mean += torch.mean(u, dim=(0, 2))
        y_mean += torch.mean(y, dim=(0, 2))
        u_var += torch.mean(torch.var(u, dim=2, unbiased=False), dim=(0, ))
        y_var += torch.mean(torch.var(y, dim=2, unbiased=False), dim=(0, ))

    u_mean = u_mean.numpy()
    y_mean = y_mean.numpy()
    u_var = u_var.numpy()
    y_var = y_var.numpy()

    u_normalizer = Normalizer1D(np.sqrt(u_var/total_batches)*variance_scaler, u_mean/total_batches)
    y_normalizer = Normalizer1D(np.sqrt(y_var/total_batches)*variance_scaler, y_mean/total_batches)
    return u_normalizer, y_normalizer


def run(options=None, load_model=None, mode_interactive=True):
    if options is None:
        options = {}

    if not mode_interactive:
        ctime = time.strftime("%c")
        run_path = get_run_path(options, ctime)
        # Create folder
        os.makedirs(run_path, exist_ok=True)
        # Set stdout to print to file and console
        set_redirects(run_path)

    if load_model is not None:
        ckpt_options = create_full_options_dict(os.path.join(os.path.dirname(load_model), 'options.txt'))

        options["model"] = ckpt_options["model"]
        options["dataset"] = ckpt_options["dataset"]

        options["optimizer"] = ckpt_options["optimizer"]
        options["model_options"] = ckpt_options["model_options"]
        options["dataset_options"] = ckpt_options["dataset_options"]

        options["normalize"] = ckpt_options["normalize"]
        options["normalize_n_std"] = ckpt_options["normalize_n_std"]

        options = recursive_merge(ckpt_options, options)
    else:
        options = create_full_options_dict(options)  # Fill with default values

    # Specifying datasets
    loaders = loader.load_dataset(dataset=options["dataset"],
                                  dataset_options=options["dataset_options"],
                                  train_batch_size=options["train_options"]["batch_size"],
                                  test_batch_size=options["test_options"]["batch_size"])

    # Compute normalizers
    if options["normalize"]:
        normalizer_input, normalizer_output = compute_normalizers(loaders['train'], options["normalize_n_std"])
    else:
        normalizer_input = normalizer_output = None

    # Define model
    modelstate = ModelState(seed=options["seed"],
                            nu=loaders["train"].nu, ny=loaders["train"].ny,
                            optimizer=options["optimizer"],
                            init_lr=options["train_options"]["init_lr"],
                            model=options["model"],
                            model_options=options["model_options"],
                            normalizer_input=normalizer_input,
                            normalizer_output=normalizer_output)

    if options["cuda"]:
        modelstate.model.cuda()

    # Restore model
    if load_model is not None:
        current_epoch = modelstate.load_model(load_model)
    else:
        current_epoch = 0

    if not mode_interactive:
        print("Training starting at: "+ctime)

        with open(os.path.join(run_path, "options.txt"), "w+") as f:
            f.write(json.dumps(options, indent=1))
            print(json.dumps(options, indent=1))

        # Run model
        train.run_train(start_epoch=current_epoch,
                        cuda=options["cuda"],
                        modelstate=modelstate,
                        logdir=run_path,
                        loader_train=loaders["train"],
                        loader_valid=loaders["valid"],
                        train_options=options["train_options"])
    else:
        return modelstate.model, loaders, options


if __name__ == "__main__":
    # Get options
    commandline_options, option_dict, option_file = get_commandline_args()
    run_options = create_full_options_dict(commandline_options, option_dict, option_file)
    # Run
    run(run_options, load_model=run_options["load_model"], mode_interactive=False)
