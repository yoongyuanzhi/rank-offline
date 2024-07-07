import json
from pathlib import Path
from tokenizers import AddedToken, Tokenizer
import onnxruntime as ort
import numpy as np
import os
from flashrank.Config import default_model, default_cache_dir
import collections
from typing import Optional, List, Dict, Any
import logging

class RerankRequest:
    """ Represents a reranking request with a query and a list of passages. 
    
    Attributes:
        query (Optional[str]): The query for which the passages need to be reranked.
        passages (List[Dict[str, Any]]): The list of passages to be reranked.
    """

    def __init__(self, query: Optional[str] = None, passages: Optional[List[Dict[str, Any]]] = None):
        self.query: Optional[str] = query
        self.passages: List[Dict[str, Any]] = passages if passages is not None else []

class Ranker:
    """ A ranker class for reranking passages based on a provided query using a pre-trained model.

    Attributes:
        cache_dir (Path): Path to the cache directory where models are stored.
        model_dir (Path): Path to the directory of the specific model being used.
        session (ort.InferenceSession): The ONNX runtime session for making inferences.
        tokenizer (Tokenizer): The tokenizer for text processing.
    """

    def __init__(self, model_name: str = default_model, cache_dir: str = default_cache_dir, max_length: int = 512, log_level: str = "INFO"):
        """ Initializes the Ranker class with specified model and cache settings.

        Args:
            model_name (str): The name of the model to be used.
            cache_dir (str): The directory where models are cached.
            max_length (int): The maximum length of the tokens.
            log_level (str): Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        """
        
        # Setting up logging
        logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))
        self.logger = logging.getLogger(__name__)

        self.cache_dir: Path = Path(cache_dir)
        self.model_dir: Path = self.cache_dir / model_name
        
        model_file = "flashrank-MiniLM-L-12-v2_Q.onnx"
        self.session = ort.InferenceSession(str(self.model_dir / model_file))
        self.tokenizer: Tokenizer = self._get_tokenizer(max_length)


    def _get_tokenizer(self, max_length: int = 512) -> Tokenizer:
        """ Initializes and configures the tokenizer with padding and truncation.

        Args:
            max_length (int): The maximum token length for truncation.

        Returns:
            Tokenizer: Configured tokenizer for text processing.
        """
        config = json.load(open(str(self.model_dir / "config.json")))
        tokenizer_config = json.load(open(str(self.model_dir / "tokenizer_config.json")))
        tokens_map = json.load(open(str(self.model_dir / "special_tokens_map.json")))
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))

        tokenizer.enable_truncation(max_length=min(tokenizer_config["model_max_length"], max_length))
        tokenizer.enable_padding(pad_id=config["pad_token_id"], pad_token=tokenizer_config["pad_token"])

        for token in tokens_map.values():
            if isinstance(token, str):
                tokenizer.add_special_tokens([token])
            elif isinstance(token, dict):
                tokenizer.add_special_tokens([AddedToken(**token)])

        vocab_file = self.model_dir / "vocab.txt"
        if vocab_file.exists():
            tokenizer.vocab = self._load_vocab(vocab_file)
            tokenizer.ids_to_tokens = collections.OrderedDict([(ids, tok) for tok, ids in tokenizer.vocab.items()])
        return tokenizer

    def _load_vocab(self, vocab_file: Path) -> Dict[str, int]:
        """ Loads the vocabulary from a file and returns it as an ordered dictionary.

        Args:
            vocab_file (Path): The file path to the vocabulary.

        Returns:
            Dict[str, int]: An ordered dictionary mapping tokens to their respective indices.
        """
        vocab = collections.OrderedDict()
        with open(vocab_file, "r", encoding="utf-8") as reader:
            tokens = reader.readlines()
        for index, token in enumerate(tokens):
            token = token.rstrip("\n")
            vocab[token] = index
        return vocab
    



    def rerank(self, request: RerankRequest) -> List[Dict[str, Any]]:
        """ Reranks a list of passages based on a query using a pre-trained model.

        Args:
            request (RerankRequest): The request containing the query and passages to rerank.

        Returns:
            List[Dict[str, Any]]: The reranked list of passages with added scores.
        """
        query = request.query
        passages = request.passages

    
        self.logger.debug("Running pairwise ranking..")
        query_passage_pairs = [[query, passage["text"]] for passage in passages]

        input_text = self.tokenizer.encode_batch(query_passage_pairs)
        input_ids = np.array([e.ids for e in input_text])
        token_type_ids = np.array([e.type_ids for e in input_text])
        attention_mask = np.array([e.attention_mask for e in input_text])

        use_token_type_ids = token_type_ids is not None and not np.all(token_type_ids == 0)

        onnx_input = {"input_ids": input_ids.astype(np.int64), "attention_mask": attention_mask.astype(np.int64)}
        if use_token_type_ids:
            onnx_input["token_type_ids"] = token_type_ids.astype(np.int64)

        outputs = self.session.run(None, onnx_input)

        logits = outputs[0]

        if logits.shape[1] == 1:
            scores = 1 / (1 + np.exp(-logits.flatten()))
        else:
            exp_logits = np.exp(logits)
            scores = exp_logits[:, 1] / np.sum(exp_logits, axis=1)

        for score, passage in zip(scores, passages):
            passage["score"] = score

        passages.sort(key=lambda x: x["score"], reverse=True)
        return passages
