# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import os
import sys
import time
import torch
import torch.distributed as dist
import torch.multiprocessing as mp

nlp_path = os.path.abspath("../../")
if nlp_path not in sys.path:
    sys.path.insert(0, nlp_path)

sys.path.insert(0, "./")
from utils_nlp.dataset.cnndm import CNNDMBertSumProcessedData, CNNDMSummarizationDataset
from utils_nlp.models.transformers.extractive_summarization import (
    ExtractiveSummarizer,
    ExtSumProcessedData,
    ExtSumProcessor,
)

# os.environ["NCCL_BLOCKING_WAIT"] = "1"

os.environ["NCCL_IB_DISABLE"] = "0"
os.environ['OMP_NUM_THREADS'] = str(torch.cuda.device_count())
os.environ["KMP_AFFINITY"] = "verbose"

parser = argparse.ArgumentParser()
parser.add_argument(
    "--rank", type=int, default=0, help="The rank of the current node in the cluster"
)
parser.add_argument(
    "--dist_url",
    type=str,
    default="tcp://127.0.0.1:29501",
    help="URL specifying how to initialize the process groupi.",
)
parser.add_argument(
    "--node_count", type=int, default=1, help="Number of nodes in the cluster."
)
parser.add_argument(
    "--cache_dir", type=str, default="./", help="Directory to cache the tokenizer."
)
parser.add_argument(
    "--data_dir",
    type=str,
    default="./",
    help="Directory to download the preprocessed data.",
)
parser.add_argument(
    "--output_dir",
    type=str,
    default="./",
    help="Directory to save the output model and prediction results.",
)
parser.add_argument(
    "--quick_run",
    type=str.lower,
    default="false",
    choices=["true", "false"],
    help="Whether to have a quick run",
)
parser.add_argument(
    "--model_name",
    type=str,
    default="distilbert-base-uncased",
    help='Transformer model used in the extractive summarization, only \
                        "bert-uncased" and "distilbert-base-uncased" are supported.',
)
parser.add_argument(
    "--encoder",
    type=str.lower,
    default="transformer",
    choices=["baseline", "classifier", "transformer", "rnn"],
    help="Encoder types in the extractive summarizer.",
)
parser.add_argument(
    "--max_pos_length",
    type=int,
    default=512,
    help="maximum input length in terms of input token numbers in training",
)
parser.add_argument("--learning_rate", type=float, default=1e-3, help="Learning rate.")
parser.add_argument(
    "--batch_size",
    type=int,
    default=5,
    help="batch size in terms of the number of samples in training",
    # default=3000,
    # help="batch size in terms of input token numbers in training",
)
parser.add_argument(
    "--max_steps",
    type=int,
    default=1e4,
    help="Maximum number of training steps run in training. If quick_run is set,\
                        it's not used.",
)
parser.add_argument(
    "--warmup_steps",
    type=int,
    default=5e3,
    help="Warm-up number of training steps run in training. If quick_run is set,\
                        it's not used.",
)
parser.add_argument(
    "--top_n",
    type=int,
    default=3,
    help="Number of sentences selected in prediction for evaluation.",
)
parser.add_argument(
    "--summary_filename",
    type=str,
    default="generated_summaries.txt",
    help="Summary file name generated by prediction for evaluation.",
)
parser.add_argument(
    "--model_filename",
    type=str,
    default="dist_extsum_model.pt",
    help="model file name saved for evaluation.",
)
parser.add_argument(
    "--train_file",
    type=str,
    default=None,
    help="training data file which is saved through torch",
)
parser.add_argument(
    "--test_file",
    type=str,
    default=None,
    help="test data file for evaluation.",
)



def cleanup():
    dist.destroy_process_group()


# How often the statistics reports show up in training, unit is step.
REPORT_EVERY = 100
SAVE_EVERY = 1000


def main():
    print("NCCL_IB_DISABLE: {}".format(os.getenv("NCCL_IB_DISABLE")))
    args = parser.parse_args()
    print("quick_run is {}".format(args.quick_run))
    print("output_dir is {}".format(args.output_dir))
    print("data_dir is {}".format(args.data_dir))
    print("cache_dir is {}".format(args.cache_dir))

    # shutil.rmtree(args.output_dir)
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(args.cache_dir, exist_ok=True)

    ngpus_per_node = torch.cuda.device_count()
    processor = ExtSumProcessor(model_name=args.model_name)
    summarizer = ExtractiveSummarizer(
        processor, args.model_name, args.encoder, args.max_pos_length, args.cache_dir
    )

    mp.spawn(
        main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, summarizer, args)
    )


def main_worker(local_rank, ngpus_per_node, summarizer, args):
    rank = args.rank * ngpus_per_node + local_rank
    world_size = args.node_count * ngpus_per_node

    print("init_method: {}".format(args.dist_url))
    print("ngpus_per_node: {}".format(ngpus_per_node))
    print("rank: {}".format(rank))
    print("local_rank: {}".format(local_rank))
    print("world_size: {}".format(world_size))

    torch.distributed.init_process_group(
        backend="nccl", init_method=args.dist_url, world_size=world_size, rank=rank,
    )
    # total number of steps for training
    MAX_STEPS = 1e1
    # number of steps for warm up
    WARMUP_STEPS = 5e2
    TOP_N = 10
    if args.quick_run.lower() == "false":
        MAX_STEPS = args.max_steps
        WARMUP_STEPS = args.warmup_steps
        TOP_N = -1

    print("max steps is {}".format(MAX_STEPS))
    print("warmup steps is {}".format(WARMUP_STEPS))

    if local_rank not in [-1, 0]:
        torch.distributed.barrier()

    # download_path = CNNDMBertSumProcessedData.download(local_path=args.data_dir)
    # ext_sum_train, ext_sum_train = ExtSumProcessedData().splits(
    #    root=download_path, train_iterable=True
    # )
    if args.train_file is None or args.test_file is None:
        train_dataset, test_dataset = CNNDMSummarizationDataset(
            top_n=TOP_N, local_cache_path=args.data_dir
        )
        ext_sum_train = summarizer.processor.preprocess(train_dataset, oracle_mode="greedy")
        ext_sum_test = summarizer.processor.preprocess(test_dataset, oracle_mode="greedy")
    else:
        ext_sum_train = torch.load(os.path.join(args.data_dir, args.train_file))
        ext_sum_test = torch.load(os.path.join(args.data_dir, args.test_file))

    if local_rank in [-1, 0]:
        torch.distributed.barrier()

    start = time.time()

    if rank not in [-1, 0]:
        save_every = -1
    else:
        save_every = SAVE_EVERY
    # """
    print("starting training")
    summarizer.fit(
        ext_sum_train,
        num_gpus=world_size,
        batch_size=args.batch_size,
        gradient_accumulation_steps=1,
        max_steps=MAX_STEPS / world_size,
        learning_rate=args.learning_rate,
        warmup_steps=WARMUP_STEPS,
        verbose=True,
        report_every=REPORT_EVERY,
        clip_grad_norm=False,
        local_rank=local_rank,
        save_every=save_every,
        world_size=world_size,
        rank=rank,
        # use_preprocessed_data=True
    )
    end = time.time()
    print("rank {0}, duration {1:.6f}s".format(rank, end - start))
    # """
    torch.distributed.barrier()
    if local_rank in [-1, 0] and args.rank == 0:
        summarizer.save_model(os.path.join(args.output_dir, args.model_filename))
        prediction = summarizer.predict(ext_sum_test[0:TOP_N], batch_size=128)

        def _write_list_to_file(list_items, filename):
            with open(filename, "w") as filehandle:
                # for cnt, line in enumerate(filehandle):
                for item in list_items:
                    filehandle.write("%s\n" % item)

        print("writing generated summaries")
        _write_list_to_file(
            prediction, os.path.join(args.output_dir, args.summary_filename)
        )

    # only use the following line when you use your own cluster.
    # AML distributed training run cleanup for you.
    cleanup()


if __name__ == "__main__":
    main()
