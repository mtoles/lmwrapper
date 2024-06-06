import sqlite3
import datetime
import base64
import xxhash
from lmwrapper.abstract_predictor import LmPredictor
from lmwrapper.caching import cache_dir
from lmwrapper.openai_wrapper import get_open_ai_lm
from lmwrapper.structs import LmPrediction, LmPrompt
import os

_text_hash_len = 32
_text_and_sample_hash_len = 43

cache_path_fn = lambda: cache_dir() / "lm_cache.db"


def execute_query(
    query: str | list[str | tuple[str, tuple[any, ...]]] | tuple[str, tuple[any, ...]],
    fetchone=False
):
    with sqlite3.connect(cache_path_fn()) as conn:
        cursor = conn.cursor()
        if isinstance(query, str):
            cursor.execute(query)
        if isinstance(query, tuple):
            assert len(query) == 2
            cursor.execute(*query)
        if isinstance(query, list):
            for q in query:
                if isinstance(q, str):
                    cursor.execute(q)
                elif isinstance(q, tuple):
                    assert len(q) == 2
                    assert isinstance(q[0], str)
                    assert isinstance(q[1], tuple)
                    cursor.execute(*q)
                else:
                    raise ValueError(f"Unexpected query type {type(q)}")
        conn.commit()
        if fetchone:
            return cursor.fetchone()
        return cursor


def create_tables():
    # TODO: if the file name chances during this process probably need to re-run this
    if cache_path_fn().exists():
        return
    if not os.path.exists(cache_dir()):
        cache_dir().mkdir()

    create_tables_sql = [
        "BEGIN;",
        """
        CREATE TABLE IF NOT EXISTS CacheLmPromptText (
            text_hash TEXT PRIMARY KEY,
            text TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CacheLmPromptSampleParams (
            text_hash TEXT,
            sample_hash TEXT PRIMARY KEY,
            model_key TEXT,
            max_tokens INTEGER,
            temperature REAL,
            top_p REAL,
            presence_penalty REAL,
            frequency_penalty REAL,
            add_bos_token TEXT,
            echo INTEGER,
            add_special_tokens INTEGER,
            has_internals_request INTEGER,
            stop TEXT,
            FOREIGN KEY (text_hash) REFERENCES CacheLmPromptText (text_hash)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CacheLmPrediction (
            sample_params TEXT,
            base_class TEXT,
            completion_text TEXT,
            metad_bytes BLOB,
            date_added TEXT
        );
        """,
    ]
    execute_query(create_tables_sql)


def prompt_to_text_hash(prompt: LmPrompt) -> str:
    text = prompt.get_text_as_string_default_form()
    hasher = xxhash.xxh64()
    hasher.update(text.encode())
    text_hash = base64.b64encode(hasher.digest()).decode()
    remaining_chars = _text_hash_len - len(text_hash)
    start_chars = text[:min(remaining_chars // 3, len(text))]
    end_chars = text[-min(remaining_chars - len(start_chars), len(text)):]
    text_hash = start_chars + end_chars[::-1] + text_hash
    if len(text_hash) < _text_hash_len:
        text_hash += "_" * (_text_hash_len - len(text_hash))
    return text_hash


def prompt_to_sample_hash_text(prompt: LmPrompt, model_key: str) -> str:
    return prompt_to_text_hash(prompt) + prompt_to_sample_params_hash(prompt, model_key)


def prompt_to_sample_params_hash(prompt: LmPrompt, model_key: str) -> str:
    _target_len = _text_and_sample_hash_len - _text_hash_len
    hasher = xxhash.xxh64()
    hasher.update(str(prompt_to_only_sample_class_dict(prompt, model_key)).encode())
    hash = base64.b64encode(hasher.digest()).decode()
    if len(hash) < _target_len:
        hash += "_" * (_target_len - len(hash))
    elif len(hash) > _target_len:
        hash = hash[:_target_len]
    return hash


def prompt_to_only_sample_class_dict(prompt: LmPrompt, model_key: str) -> dict:
    return dict(
        model_key=model_key,
        max_tokens=prompt.max_tokens,
        temperature=prompt.temperature,
        top_p=prompt.top_p,
        presence_penalty=prompt.presence_penalty,
        frequency_penalty=prompt.frequency_penalty,
        add_bos_token=str(prompt.add_bos_token),
        echo=prompt.echo,
        add_special_tokens=prompt.add_special_tokens,
        has_internals_request=prompt.model_internals_request is not None,
        stop=str(prompt.stop),
    )


def create_from_prompt_text(prompt: LmPrompt):
    text_hash = prompt_to_text_hash(prompt)
    text = prompt.get_text_as_string_default_form()
    execute_query("INSERT OR IGNORE INTO CacheLmPromptText (text_hash, text) VALUES (?, ?)",
                  (text_hash, text))
    return text_hash


def create_from_prompt_sample_params(prompt: LmPrompt, model_key: str):
    text_hash = create_from_prompt_text(prompt)
    sample_hash = prompt_to_sample_hash_text(prompt, model_key)
    params = prompt_to_only_sample_class_dict(prompt, model_key)
    execute_query((
        """
        INSERT OR IGNORE INTO CacheLmPromptSampleParams 
        (text_hash, sample_hash, model_key, max_tokens, temperature, top_p, presence_penalty, 
        frequency_penalty, add_bos_token, echo, add_special_tokens, has_internals_request, stop) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            text_hash, sample_hash, params['model_key'], params['max_tokens'], params['temperature'],
            params['top_p'], params['presence_penalty'], params['frequency_penalty'], params['add_bos_token'],
            params['echo'], params['add_special_tokens'], params['has_internals_request'], params['stop']
        )
    ))
    return sample_hash


def add_prediction_to_cache(prediction: LmPrediction, model_key: str):
    create_tables()
    sample_hash = prompt_to_sample_hash_text(prediction.prompt, model_key)
    params = prompt_to_only_sample_class_dict(prediction.prompt, model_key)
    text_hash = prompt_to_text_hash(prediction.prompt)
    text = prediction.prompt.get_text_as_string_default_form()

    execute_query([
        "BEGIN;",
        ("INSERT OR IGNORE INTO CacheLmPromptText (text_hash, text) VALUES (?, ?);", (text_hash, text)),
        (
            """
            INSERT OR IGNORE INTO CacheLmPromptSampleParams 
            (text_hash, sample_hash, model_key, max_tokens, temperature, top_p, presence_penalty, 
            frequency_penalty, add_bos_token, echo, add_special_tokens, has_internals_request, stop) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                text_hash, sample_hash, params['model_key'], params['max_tokens'], params['temperature'],
                params['top_p'], params['presence_penalty'], params['frequency_penalty'],
                params['add_bos_token'],
                params['echo'], params['add_special_tokens'], params['has_internals_request'], params['stop']
            )
        ),
        (
            """
            INSERT INTO CacheLmPrediction 
            (sample_params, base_class, completion_text, metad_bytes, date_added) 
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                sample_hash, prediction.__class__.__name__,
                prediction.completion_text, prediction.serialize_metad_for_cache(),
                datetime.datetime.now().isoformat()
            )
        ),
    ])


def get_from_cache(prompt: LmPrompt, lm: LmPredictor = None) -> LmPrediction | None:
    create_tables()
    sample_hash = prompt_to_sample_hash_text(prompt, lm.get_model_cache_key())
    with sqlite3.connect(cache_path_fn()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM CacheLmPrediction WHERE sample_params = ?", (sample_hash,)
        )
        ret = cursor.fetchone()
        if not ret:
            return None
        assert len(ret) == 5
        completion_text = ret[2]
        metad_bytes = ret[3]
    return lm.find_prediction_class(prompt).parse_from_cache(completion_text, prompt, metad_bytes)


def main():
    create_tables()
    lm = get_open_ai_lm()
    pred = lm.predict("Once upon a time")
    add_prediction_to_cache(pred, lm.get_model_cache_key())


class SqlBackedCache:
    def __init__(self, lm):
        create_tables()
        self._lm = lm

    def __contains__(self, prompt: LmPrompt):
        return get_from_cache(prompt, self._lm) is not None

    def get(self, prompt: LmPrompt):
        return get_from_cache(prompt, self._lm)

    def add(self, prediction: LmPrediction):
        add_prediction_to_cache(prediction, self._lm.get_model_cache_key())

    def delete(self, prompt: LmPrompt) -> bool:
        if not isinstance(prompt, LmPrompt):
            raise ValueError(f"Expected LmPrompt, got {type(prompt)}")
        sample_hash = prompt_to_sample_hash_text(prompt, self._lm.get_model_cache_key())
        with sqlite3.connect(cache_path_fn()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM CacheLmPrediction WHERE sample_params = ?", (sample_hash,)
            )
            cursor.execute(
                "DELETE FROM CacheLmPromptSampleParams WHERE sample_hash = ?", (sample_hash,)
            )
            conn.commit()
            data_deleted = cursor.rowcount > 0
            # Delete the text hash if no longer used
            text_hash = prompt_to_text_hash(prompt)
            cursor.execute(
                "SELECT COUNT(*) FROM CacheLmPromptSampleParams WHERE text_hash = ?",
                (text_hash,)
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "DELETE FROM CacheLmPromptText WHERE text_hash = ?",
                    (text_hash,)
                )
                conn.commit()
        return data_deleted


if __name__ == "__main__":
    main()
