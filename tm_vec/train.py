import os
import argparse
import inspect
from pathlib import Path
from dataclasses import fields, dataclass, field

import pickle
import numpy as np

import torch
from torch.utils.data import DataLoader
import lightning as L
from lightning.pytorch.loggers import WandbLogger
import gc

from tm_vec.dataset import collate_fn, tm_score_embeds_dataset, construct_datasets
from tm_vec.model import trans_basic_block, trans_basic_block_Config
from tm_vec.utils import SessionTree


#Comand line
def arguments():
    parser = argparse.ArgumentParser(description="Train a structural embedding model")

    parser.add_argument("--gpus",
            type=int,
            help="Num. gpus",
            default=1
    )
    parser.add_argument("--nodes",
            type=int,
            help="Num. nodes",
            default=1
    )

    parser.add_argument("--hdf_file",
                type=Path,
                required=True,
                help="HDF file with embeddings"
        )

    parser.add_argument("--tm_pairs",
                type=Path,
                required=True,
                help="TSV file with pairs of proteins with TM scores"
        )

    parser.add_argument("--session",
            type=Path,
            required=True,
            help="Training session directory; models are saved here along with other important metadata"
    )

    parser.add_argument("--batch-size",
            type=int,
            help="Batch size",
            default=16
    )
    parser.add_argument("--max-epochs",
            type=int,
            help="Epochs",
            default=16
    )
    parser.add_argument("--seed",
            type=int,
            help="Random seed",
            default=1230
    )
    parser.add_argument("--train-prop",
            type=float,
            default=0.9,
            help="Proportion of dataset used to train"
    )
    parser.add_argument("--val-prop",
            type=float,
            default=0.05,
            help="Proportion of data to use for validation"
    )

    parser.add_argument("--test-prop",
            type=float,
            default=0.05,
            help="Proportion of data to use for test"
    )

    # Now add the transformer model arguments
    for field in fields(trans_basic_block_Config):
        parser.add_argument(
            f"--{field.name}", default=field.default, type=field.type
        )

    return parser.parse_args()

def collect_trans_block_arguments(args) -> trans_basic_block_Config:
        trans_block_conf_args = inspect.signature(trans_basic_block_Config).parameters
        return {k: v for k, v in args.items() if k in trans_block_conf_args}


if __name__ == '__main__':

        #Construct datasets: Make train, test, and validation datasets
        args = arguments()
        config = collect_trans_block_arguments(vars(args))
        config = trans_basic_block_Config(**config)
        print(config, flush=True)
        model = config.build()

        tree = SessionTree(args.session)
        config.to_json(tree.params)

        train_ds, val_ds, test_ds = construct_datasets(args.hdf_file, args.tm_pairs,
                                                       args.train_prop, args.val_prop, args.test_prop)

        print("Constructed datasets")
        #Build the data loaders: train data loader and validation data loader
        train_dataloader = DataLoader(train_ds, batch_size=args.batch_size, collate_fn=collate_fn, num_workers=2)
        val_dataloader = DataLoader(val_ds, batch_size=args.batch_size, collate_fn=collate_fn, num_workers=2)

        val_check_interval = 0.05
        effective_batch_size = args.gpus * args.nodes * args.batch_size
        every_n_train_steps = int(len(train_ds) * val_check_interval) // effective_batch_size
        print("Saving and validating every ", every_n_train_steps, " steps")

        #Model checkpoints
        ckpt = L.pytorch.callbacks.ModelCheckpoint(
                dirpath=tree.checkpoints,
                monitor="val_loss",
                verbose=True,
                filename="{epoch}-{step}-{val_loss:0.4f}",
                every_n_train_steps=every_n_train_steps,
                save_top_k=5,
                save_weights_only=False,
                save_last=True
        )

        # logger = pl.loggers.TensorBoardLogger(tree.logs)
        logger = WandbLogger(project="tm_vec", log_model=False, save_dir=tree.logs, offline=True,
                             config=config)
        # Trainer
        trainer = L.Trainer(
                strategy='ddp',
                accelerator='gpu',
                callbacks=[ckpt],
                logger=logger,
                precision="16-true",
                devices=args.gpus,
                val_check_interval=val_check_interval,
                num_nodes=args.nodes,
                gradient_clip_val=0.5,
                gradient_clip_algorithm="norm",
                max_epochs=args.max_epochs
        )


        # Setup model and fit
        print("Training...")
        trainer.fit(model, train_dataloader, val_dataloader)
        print("Training complete")