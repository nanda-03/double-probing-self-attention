import torch
import torch.nn as nn
import torch.optim as optim
from .utils import slice_transformers
from transformers import AutoConfig
import pytorch_lightning as pl

OPTMIZER_DIC = {"Adam": optim.Adam}


class DpsaModel(nn.Module):
    def __init__(
        self,
        model_name: str,
        pivot: int,
        dropout_reducer: float,
        num_layer_reducer: int,
        num_class: int,
    ):
        super(DpsaModel, self).__init__()
        self.base_model, self.cross_model = slice_transformers(model_name, pivot)
        config = AutoConfig.from_pretrained(model_name)
        self.reducer = nn.GRU(
            config.hidden_size,
            config.hidden_size,
            num_layer_reducer,
            dropout=dropout_reducer,
            batch_first=True,
        )
        self.dropout = nn.Dropout(dropout_reducer)
        self.linear = nn.Linear(2 * config.hidden_size, num_class)

    def forward(
        self,
        premise_input_ids,
        premise_attention_mask,
        hypothesis_input_ids,
        hypothesis_attention_mask,
    ):
        premise_hidden_state = self.base_model(
            input_ids=premise_input_ids, attention_mask=premise_attention_mask
        ).last_hidden_state
        hypothesis_hidden_state = self.base_model(
            input_ids=hypothesis_input_ids, attention_mask=hypothesis_attention_mask
        ).last_hidden_state

        premise_hypothesis = self.cross_model(
            hidden_states=premise_hidden_state,
            encoder_hidden_states=hypothesis_hidden_state,
            encoder_attention_mask=hypothesis_attention_mask,
        ).last_hidden_state

        hypothesis_premise = self.cross_model(
            hidden_states=hypothesis_hidden_state,
            encoder_hidden_states=premise_hidden_state,
            encoder_attention_mask=premise_attention_mask,
        ).last_hidden_state
        
        premise_hypothesis = self.dropout(premise_hypothesis)
        hypothesis_premise = self.dropout(hypothesis_premise)

        premise_hypothesis_pooler = self.reducer(premise_hypothesis)[0][:, -1, :]
        hypothesis_premise_pooler = self.reducer(hypothesis_premise)[0][:, -1, :]
        
        output = torch.cat(
            [premise_hypothesis_pooler, hypothesis_premise_pooler], dim=-1
        )
        output = self.linear(output)

        return output


class DpsaLightningModule(pl.LightningModule):

    criterion = nn.CrossEntropyLoss()

    def __init__(
        self,
        model_name,
        pivot,
        dropout_reducer,
        num_layer_reducer,
        num_class,
        learning_rate,
        lr_factor,
        lr_schedule_patience,
        optimizer_name,
    ):
        super(DpsaLightningModule, self).__init__()
        self.model = DpsaModel(
            model_name, pivot, dropout_reducer, num_layer_reducer, num_class
        )
        self.learning_rate = learning_rate
        self.lr_factor = lr_factor
        self.lr_schedule_patience = lr_schedule_patience
        self.optimizer_name = optimizer_name

    def configure_optimizers(self):
        optimizer = OPTMIZER_DIC.get(self.optimizer_name, optim.Adam)(
            self.model.parameters(), lr=self.learning_rate
        )
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, "min", factor=self.lr_factor, patience=self.lr_schedule_patience
        )
        output = {
            "optimizer": optimizer,
            "lr_scheduler": scheduler,
            "monitor": "val_loss",
        }
        return output

    def _metric_forward(self, batch):
        (
            premise_input_ids,
            premise_attention_mask,
            hypothesis_input_ids,
            hypothesis_attention_mask,
            label,
        ) = (
            batch["premise_input_ids"],
            batch["premise_attention_mask"],
            batch["hypothesis_input_ids"],
            batch["hypothesis_attention_mask"],
            batch["label"],
        )
        logits = self.model(
            premise_input_ids,
            premise_attention_mask,
            hypothesis_input_ids,
            hypothesis_attention_mask,
        )
        label = label.long()
        loss = self.criterion(logits, label)
        prediction = torch.argmax(logits, dim=-1)
        accuracy = (prediction == label).float().mean()

        return loss, accuracy

    def training_step(self, batch, batch_idx):
        loss, accuracy = self._metric_forward(batch)
        output = {"loss": loss, "train_accuracy": accuracy}
        self.log_dict(output)
        return output

    def validation_step(self, batch, batch_idx):
        loss, accuracy = self._metric_forward(batch)
        output = {"val_loss": loss, "val_accucary": accuracy}
        self.log_dict(output)
        return output

    def test_step(self, batch, batch_idx):
        loss, accuracy = self._metric_forward(batch)
        output = {"test_loss": loss, "test_accuracy": accuracy}
        self.log_dict(output)
        return output
