"""Assign notes to a set of predetermined topics.

The topic assignment process is seeded with a small set of terms which are indicative of
the topic.  After preliminary topic assignment based on term matching, a logistic regression
trained on bag-of-words features model expands the set of in-topic notes for each topic.
Note that the logistic regression modeling excludes any tokens containing seed terms.

This approach represents a prelimiary approach to topic assignment while Community Notes
evaluates the efficacy of per-topic note scoring.
"""

import logging
import re
from typing import List, Optional, Tuple

from . import constants as c
from .enums import Topics

import numpy as np
import pandas as pd
from scipy.special import expit as sigmoid, softmax
from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score
from sklearn.pipeline import Pipeline


logger = logging.getLogger("birdwatch.topic_model")
logger.setLevel(logging.INFO)


class TopicModel(object):
  def __init__(self, unassignedThreshold=0.85):
    """Initialize a list of seed terms for each topic."""
    self._seedTerms = {
      Topics.UkraineConflict: {
        "ukrain",  # intentionally shortened for expanded matching
        "russia",
        "kiev",
        "kyiv",
        "moscow",
        "zelensky",
        "putin",
      },
      Topics.GazaConflict: {
        "israel",
        "palestin",  # intentionally shortened for expanded matching
        "gaza",
        "jerusalem",
      },
      Topics.MessiRonaldo: {
        "messi\s",  # intentional whitespace to prevent prefix matches
        "ronaldo",
      },
    }
    self._unassignedThreshold = unassignedThreshold
    self._compiled_regex = self._compile_regex()

  def _compile_regex(self):
    """Compile a single regex from all seed terms grouped by topic."""
    regex_patterns = {}
    for topic, patterns in self._seedTerms.items():
      group_name = f"{topic.name}"
      regex_patterns[group_name] = f"(?P<{group_name}>{'|'.join(patterns)})"

    combined_regex = "|".join(regex_patterns.values())
    return re.compile(combined_regex, re.IGNORECASE)

  def _make_seed_labels(self, texts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Produce a label vector based on seed terms.

    Args:
      texts: array containing strings for topic assignment

    Returns:
      Tuple[0]: array specifying topic labels for texts
      Tuple[1]: array specifying texts that are unassigned due to conflicting matches.
    """
    labels = np.zeros(texts.shape[0], dtype=np.int64)
    conflictedTexts = np.zeros(texts.shape[0], dtype=bool)

    for i, text in enumerate(texts):
      matches = self._compiled_regex.finditer(text.lower())
      found_topics = set()
      for match in matches:
        found_topics.update([Topics[grp].value for grp in match.groupdict() if match.group(grp)])

      if len(found_topics) == 1:
        labels[i] = found_topics.pop()
      elif len(found_topics) > 1:
        labels[i] = Topics.Unassigned.value
        conflictedTexts[i] = True

    unassigned_count = np.sum(conflictedTexts)
    logger.info(f"  Notes unassigned due to multiple matches: {unassigned_count}")
    return labels, conflictedTexts

  def _get_stop_words(self, texts: np.ndarray) -> List[str]:
    """Identify tokens in the extracted vocabulary that contain seed terms.

    Any token containing a seed term will be treated as a stop word (i.e. removed
    from the extracted features).  This prevents the model from training on the same
    tokens used to label the data.

    Args:
      texts: array containing strings for topic assignment

    Returns:
      List specifying which tokens to exclude from the features.
    """
    # Extract vocabulary
    cv = CountVectorizer(strip_accents="unicode")
    cv.fit(texts)
    rawVocabulary = cv.vocabulary_.keys()
    logger.info(f"  Initial vocabulary length: {len(rawVocabulary)}")
    # Identify stop words
    blockedTokens = set()
    for terms in self._seedTerms.values():
      # Remove whitespace and any escaped characters from terms
      blockedTokens |= {re.sub(r"\\.", "", t.strip()) for t in terms}
    logger.info(f"  Total tokens to filter: {len(blockedTokens)}")
    stopWords = [v for v in rawVocabulary if any(t in v for t in blockedTokens)]
    logger.info(f"  Total identified stopwords: {len(stopWords)}")
    return stopWords

  def _merge_predictions_and_labels(self, probs: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Update predictions based on defined labels when the label is not Unassigned.

    Args:
      probs: 2D matrix specifying the likelihood of each class

    Returns:
      Updated predictions based on keyword matches when available.
    """
    predictions = np.argmax(probs, axis=1)
    for label in range(1, len(Topics)):
      # Update label if (1) note was assigned based on the labeling heuristic, and (2)
      # p(Unassigned) is below the required uncertainty threshold.
      predictions[(labels == label) & (probs[:, 0] <= self._unassignedThreshold)] = label
    return predictions

  def _prepare_post_text(self, notes: pd.DataFrame) -> pd.DataFrame:
    """Concatenate all notes within each post into a single row associated with the post.

    Args:
      notes: dataframe containing raw note text

    Returns:
      DataFrame with one post per row containing note text
    """
    postNoteText = (
      notes[[c.tweetIdKey, c.summaryKey]]
      .fillna({c.summaryKey: ""})
      .groupby(c.tweetIdKey)[c.summaryKey]
      .apply(lambda postNotes: " ".join(postNotes))
      .reset_index(drop=False)
    )
    # Default tokenization for CountVectorizer will not split on underscore, which
    # results in very long tokens containing many words inside of URLs.  Removing
    # underscores allows us to keep default splitting while fixing that problem.
    postNoteText[c.summaryKey] = [
      text.replace("_", " ") for text in postNoteText[c.summaryKey].values
    ]
    return postNoteText

  def train_note_topic_classifier(
    self, notes: pd.DataFrame
  ) -> Tuple[Pipeline, np.ndarray, np.ndarray]:
    # Obtain aggregate post text, seed labels and stop words
    with c.time_block("Get Note Topics: Prepare Post Text"):
      postText = self._prepare_post_text(notes)

    with c.time_block("Get Note Topics: Make Seed Labels"):
      seedLabels, conflictedTexts = self._make_seed_labels(postText[c.summaryKey].values)

    with c.time_block("Get Note Topics: Get Stop Words"):
      stopWords = self._get_stop_words(postText[c.summaryKey].values)

    with c.time_block("Get Note Topics: Train Model"):
      # Define and fit model
      pipe = Pipeline(
        [
          (
            "UnigramEncoder",
            CountVectorizer(
              strip_accents="unicode",
              stop_words=stopWords,
              min_df=25,
              max_df=max(1000, int(0.25 * len(postText))),
            ),
          ),
          ("tfidf", TfidfTransformer()),
          ("Classifier", LogisticRegression(max_iter=1000, verbose=1)),
        ],
        verbose=True,
      )
      pipe.fit(
        # Notice that we omit posts with an unclear label from training.
        postText[c.summaryKey].values[~conflictedTexts],
        seedLabels[~conflictedTexts],
      )

    return pipe, seedLabels, conflictedTexts

  def get_note_topics(
    self,
    notes: pd.DataFrame,
    noteTopicClassifier: Optional[Pipeline] = None,
    seedLabels: Optional[np.ndarray] = None,
    conflictedTextsForAccuracyEval: Optional[np.ndarray] = None,
  ) -> pd.DataFrame:
    """Return a DataFrame specifying each {note, topic} pair.

    Notes that are not assigned to a topic do not appear in the dataframe.

    Args:
      notes: DF containing all notes to potentially assign to a topic
    """
    logger.info("Assigning notes to topics:")
    if noteTopicClassifier is not None:
      pipe = noteTopicClassifier
    else:
      logger.info("Training note topic classifier")
      pipe, seedLabels, conflictedTextsForAccuracyEval = self.train_note_topic_classifier(notes)
    postText = self._prepare_post_text(notes)

    with c.time_block("Get Note Topics: Predict"):
      # Predict notes.  Notice that in effect we are looking to see which notes in the
      # training data the model felt were mis-labeled after the training process
      # completed, and generating labels for any posts which were omitted from the
      # original training.
      logits = pipe.decision_function(postText[c.summaryKey].values)
      # Transform logits to probabilities, handling the case where logits are 1D because
      # of unit testing with only 2 topics.
      if len(logits.shape) == 1:
        probs = sigmoid(logits)
        probs = np.vstack([1 - probs, probs]).T
      else:
        probs = softmax(logits, axis=1)

    if seedLabels is None:
      with c.time_block("Get Note Topics: Make Seed Labels"):
        seedLabels, _ = self._make_seed_labels(postText[c.summaryKey].values)

    if conflictedTextsForAccuracyEval is not None:
      self.validate_note_topic_accuracy_on_seed_labels(
        np.argmax(probs, axis=1), seedLabels, conflictedTextsForAccuracyEval
      )

    with c.time_block("Get Note Topics: Merge and assign predictions"):
      topicAssignments = self._merge_predictions_and_labels(probs, seedLabels)
      logger.info(f"  Post Topic assignment results: {np.bincount(topicAssignments)}")

      # Assign topics to notes based on aggregated note text, and drop any
      # notes on posts that were unassigned.
      postText[c.noteTopicKey] = [Topics(t).name for t in topicAssignments]
      postText = postText[postText[c.noteTopicKey] != Topics.Unassigned.name]
      noteTopics = notes[[c.noteIdKey, c.tweetIdKey]].merge(
        postText[[c.tweetIdKey, c.noteTopicKey]]
      )
      logger.info(
        f"  Note Topic assignment results:\n{noteTopics[c.noteTopicKey].value_counts(dropna=False)}"
      )
    return noteTopics.drop(columns=c.tweetIdKey)

  def validate_note_topic_accuracy_on_seed_labels(self, pred, seedLabels, conflictedTexts):
    balancedAccuracy = balanced_accuracy_score(seedLabels[~conflictedTexts], pred[~conflictedTexts])
    logger.info(f"  Balanced accuracy on raw predictions: {balancedAccuracy}")
    assert balancedAccuracy > 0.5, f"Balanced accuracy too low: {balancedAccuracy}"
    # Validate that any conflicted text is Unassigned in seedLabels
    assert all(seedLabels[conflictedTexts] == Topics.Unassigned.value)